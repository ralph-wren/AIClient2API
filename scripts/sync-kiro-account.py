#!/usr/bin/env python3
"""
Import the current local Kiro OAuth token into AIClient2API, verify it, and
optionally sync the new provider node to a remote Docker deployment.

Typical PowerShell usage:

  $env:AICLIENT_REMOTE_HOST = "154.9.232.80"
  $env:AICLIENT_REMOTE_USER = "root"
  $env:AICLIENT_REMOTE_PASSWORD = "..."
  $env:AICLIENT_API_KEY = "sk-..."
  python scripts/sync-kiro-account.py --sync-remote

If you need to switch Kiro accounts first, log out/in in the Kiro client, then
run this script after ~/.aws/sso/cache/kiro-auth-token.json has changed.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

try:
    import paramiko
except ImportError:  # pragma: no cover - only needed for --sync-remote
    paramiko = None


PROVIDER_TYPE = "claude-kiro-oauth"
DEFAULT_CHECK_MODEL = "claude-haiku-4-5"


@dataclass
class LocalImportResult:
    provider: dict[str, Any]
    token_dest: Path
    added: bool
    duplicate_of: str | None = None


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def normalize_token_identity(token: dict[str, Any]) -> tuple[str | None, str | None]:
    return token.get("profileArn"), token.get("refreshToken")


def validate_kiro_token(token_path: Path) -> dict[str, Any]:
    if not token_path.exists():
        fail(f"Kiro token file not found: {token_path}")

    token = read_json(token_path)
    missing = [key for key in ("accessToken", "refreshToken", "profileArn") if not token.get(key)]
    if missing:
        fail(f"Kiro token is missing required fields: {', '.join(missing)}")

    expires_at = token.get("expiresAt")
    if expires_at:
        try:
            # Keep this as a warning: a refresh token may still allow recovery.
            expiry = time.mktime(time.strptime(expires_at[:19], "%Y-%m-%dT%H:%M:%S"))
            if expiry < time.time():
                print(f"WARN: token accessToken expired at {expires_at}; import will rely on refresh behavior.")
        except Exception:
            print(f"WARN: could not parse expiresAt: {expires_at}")

    return token


def wait_for_new_token(token_path: Path, timeout_seconds: int) -> None:
    before_mtime = token_path.stat().st_mtime if token_path.exists() else 0
    print(f"Waiting for Kiro token update: {token_path}")
    print("Switch/login the Kiro account in the Kiro client now.")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if token_path.exists() and token_path.stat().st_mtime > before_mtime:
            print("Detected updated Kiro token.")
            return
        time.sleep(2)
    fail(f"Timed out waiting for token update after {timeout_seconds} seconds")


def wait_for_token_change(token_path: Path, previous_identity: tuple[str | None, str | None] | None, timeout_seconds: int) -> dict[str, Any]:
    before_mtime = token_path.stat().st_mtime if token_path.exists() else 0
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if token_path.exists() and token_path.stat().st_mtime > before_mtime:
            token = validate_kiro_token(token_path)
            identity = normalize_token_identity(token)
            if identity != previous_identity:
                return token
            before_mtime = token_path.stat().st_mtime
            print("Detected token write, but it is the same account. Keep switching/login another account.")
        time.sleep(2)
    fail(f"Timed out waiting for a different Kiro token after {timeout_seconds} seconds")


def load_local_pools(config_root: Path) -> dict[str, Any]:
    pool_path = config_root / "provider_pools.json"
    if not pool_path.exists():
        return {}
    return read_json(pool_path)


def find_duplicate_provider(config_root: Path, token: dict[str, Any]) -> dict[str, Any] | None:
    wanted_profile, wanted_refresh = normalize_token_identity(token)
    pools = load_local_pools(config_root)

    for provider in pools.get(PROVIDER_TYPE, []):
        rel = provider.get("KIRO_OAUTH_CREDS_FILE_PATH")
        if not rel:
            continue
        cred_path = config_root.parent / rel.replace("./configs/", "configs/").replace("/", os.sep)
        if not cred_path.exists():
            continue
        try:
            existing = read_json(cred_path)
        except Exception:
            continue
        profile, refresh = normalize_token_identity(existing)
        if profile == wanted_profile and refresh == wanted_refresh:
            return provider
    return None


def login(base_url: str, admin_password: str) -> str:
    resp = requests.post(
        f"{base_url.rstrip('/')}/api/login",
        json={"password": admin_password},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success") or not data.get("token"):
        fail(f"login failed: {data}")
    return data["token"]


def add_provider(base_url: str, admin_token: str, provider: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(
        f"{base_url.rstrip('/')}/api/providers",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"providerType": PROVIDER_TYPE, "providerConfig": provider},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        fail(f"add provider failed: {data}")
    return data


def check_provider(base_url: str, admin_token: str, provider_uuid: str) -> dict[str, Any]:
    resp = requests.post(
        f"{base_url.rstrip('/')}/api/providers/{PROVIDER_TYPE}/{provider_uuid}/health-check",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("healthy"):
        fail(f"provider health check failed: {data}")
    return data


def import_local(args: argparse.Namespace, token: dict[str, Any]) -> LocalImportResult:
    config_root = Path(args.config_root).resolve()
    duplicate = None if args.force_add else find_duplicate_provider(config_root, token)

    if duplicate:
        token_rel = duplicate["KIRO_OAUTH_CREDS_FILE_PATH"]
        token_dest = config_root.parent / token_rel.replace("./configs/", "configs/").replace("/", os.sep)
        print(f"Local duplicate detected, reusing provider: {duplicate['uuid']}")
        return LocalImportResult(provider=duplicate, token_dest=token_dest, added=False, duplicate_of=duplicate["uuid"])

    timestamp = int(time.time() * 1000)
    token_dir = config_root / "kiro" / f"{timestamp}_kiro-auth-token"
    token_dir.mkdir(parents=True, exist_ok=True)
    token_dest = token_dir / f"{timestamp}_kiro-auth-token.json"
    shutil.copy2(args.token_file, token_dest)

    provider = {
        "KIRO_OAUTH_CREDS_FILE_PATH": f"./configs/kiro/{timestamp}_kiro-auth-token/{timestamp}_kiro-auth-token.json",
        "uuid": str(uuid.uuid4()),
        "checkModelName": args.check_model,
        "checkHealth": False,
        "isHealthy": True,
        "isDisabled": False,
        "lastUsed": None,
        "usageCount": 0,
        "errorCount": 0,
        "lastErrorTime": None,
        "lastHealthCheckTime": None,
        "lastHealthCheckModel": None,
        "lastErrorMessage": None,
        "needsRefresh": False,
        "refreshCount": 0,
        "customName": args.custom_name or f"kiro-local-{timestamp}",
    }

    admin_token = login(args.local_base_url, args.admin_password)
    add_provider(args.local_base_url, admin_token, provider)
    print(f"Local provider added: {provider['uuid']}")
    return LocalImportResult(provider=provider, token_dest=token_dest, added=True)


def remote_run(ssh: Any, command: str, timeout: int = 120) -> str:
    _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if code != 0:
        fail(f"remote command failed ({code}): {command}\nSTDOUT={out}\nSTDERR={err}")
    return out.strip()


def remote_read_json(sftp: Any, path: str) -> Any:
    with sftp.open(path, "r") as f:
        return json.loads(f.read().decode("utf-8"))


def remote_write_json(ssh: Any, sftp: Any, path: str, data: Any) -> None:
    tmp = f"{path}.tmp.{int(time.time())}"
    with sftp.open(tmp, "w") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
        f.write("\n")
    remote_run(ssh, f"mv {tmp!r} {path!r}")


def remote_find_duplicate(sftp: Any, remote_base: str, pools: dict[str, Any], token: dict[str, Any]) -> dict[str, Any] | None:
    wanted_profile, wanted_refresh = normalize_token_identity(token)
    for provider in pools.get(PROVIDER_TYPE, []):
        rel = provider.get("KIRO_OAUTH_CREDS_FILE_PATH")
        if not rel:
            continue
        remote_token = posixpath.join(remote_base, rel.replace("./configs/", "configs/"))
        try:
            existing = remote_read_json(sftp, remote_token)
        except Exception:
            continue
        profile, refresh = normalize_token_identity(existing)
        if profile == wanted_profile and refresh == wanted_refresh:
            return provider
    return None


def sync_remote(args: argparse.Namespace, local_result: LocalImportResult, token: dict[str, Any]) -> None:
    if paramiko is None:
        fail("paramiko is not installed; cannot use --sync-remote")

    host = args.remote_host or os.getenv("AICLIENT_REMOTE_HOST")
    user = args.remote_user or os.getenv("AICLIENT_REMOTE_USER", "root")
    password = args.remote_password or os.getenv("AICLIENT_REMOTE_PASSWORD")
    remote_base = args.remote_base_dir
    if not host or not password:
        fail("remote host/password missing. Set AICLIENT_REMOTE_HOST and AICLIENT_REMOTE_PASSWORD.")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=password, timeout=20)
    sftp = ssh.open_sftp()
    try:
        remote_pool = posixpath.join(remote_base, "configs/provider_pools.json")
        remote_token = posixpath.join(
            remote_base,
            local_result.provider["KIRO_OAUTH_CREDS_FILE_PATH"].replace("./configs/", "configs/"),
        )
        remote_run(ssh, f"mkdir -p {posixpath.dirname(remote_token)!r}")
        sftp.put(str(local_result.token_dest), remote_token)

        pools = remote_read_json(sftp, remote_pool)
        duplicate = None if args.force_add else remote_find_duplicate(sftp, remote_base, pools, token)
        if duplicate:
            print(f"Remote duplicate detected, reusing provider: {duplicate['uuid']}")
        else:
            backup = f"{remote_pool}.bak.{int(time.time())}"
            remote_run(ssh, f"cp {remote_pool!r} {backup!r}")
            pools.setdefault(PROVIDER_TYPE, [])
            if not any(p.get("uuid") == local_result.provider["uuid"] for p in pools[PROVIDER_TYPE]):
                pools[PROVIDER_TYPE].append(local_result.provider)
            remote_write_json(ssh, sftp, remote_pool, pools)
            print(f"Remote provider appended: {local_result.provider['uuid']}")
            print(f"Remote backup: {backup}")

        if args.remote_restart:
            compose = posixpath.join(remote_base, "docker-compose.yml")
            compose_text = remote_run(ssh, f"cat {compose!r}")
            service = "aiclient-api" if "aiclient-api:" in compose_text else "aiclient2api"
            remote_run(ssh, f"cd {remote_base!r} && docker compose up -d --force-recreate {service}", timeout=180)
            time.sleep(args.remote_health_wait)
            health = remote_run(
                ssh,
                "docker inspect aiclient2api --format '{{.State.Health.Status}}' "
                "2>/dev/null || docker ps --filter name=aiclient --format '{{.Names}} {{.Status}}'",
            )
            print(f"Remote container health: {health}")
    finally:
        sftp.close()
        ssh.close()


def verify_remote_models(args: argparse.Namespace) -> None:
    api_key = args.api_key or os.getenv("AICLIENT_API_KEY")
    if not api_key:
        return
    url = f"{args.remote_api_base.rstrip('/')}/{PROVIDER_TYPE}/v1/models"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.SSLError:
        curl = shutil.which("curl")
        if not curl:
            raise
        completed = subprocess.run(
            [curl, "-sS", url, "-H", f"Authorization: Bearer {api_key}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(completed.stdout)
    count = len(data.get("data", []))
    print(f"Remote models endpoint OK: {count} models")


def sync_one_token(args: argparse.Namespace, token: dict[str, Any]) -> LocalImportResult:
    local_result = import_local(args, token)

    if not args.skip_local_health_check:
        admin_token = login(args.local_base_url, args.admin_password)
        result = check_provider(args.local_base_url, admin_token, local_result.provider["uuid"])
        print(f"Local health check OK: {result.get('modelName')}")

    if args.sync_remote:
        sync_remote(args, local_result, token)
        verify_remote_models(args)

    return local_result


def watch_and_sync(args: argparse.Namespace, initial_identity: tuple[str | None, str | None] | None) -> None:
    token_path = Path(args.token_file)
    synced = 0
    current_identity = initial_identity

    print(f"Watch mode target count: {args.watch_count}")
    print("For each account: switch/login in Kiro, then wait for this script to sync it.")

    while synced < args.watch_count:
        print(f"\n[{synced + 1}/{args.watch_count}] Waiting for a different Kiro token...")
        current_token = wait_for_token_change(token_path, current_identity, args.watch_timeout)
        current_identity = normalize_token_identity(current_token)
        sync_one_token(args, current_token)
        synced += 1

    print(f"Watch mode done. Synced {synced} account(s).")


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description="Sync current Kiro account token into AIClient2API.")
    parser.add_argument("--token-file", default=str(home / ".aws/sso/cache/kiro-auth-token.json"))
    parser.add_argument("--wait-new-token", type=int, default=0, metavar="SECONDS")
    parser.add_argument("--watch-count", type=int, default=0, help="Continuously wait for and sync this many new Kiro accounts.")
    parser.add_argument("--watch-timeout", type=int, default=600, help="Timeout per account in --watch-count mode.")
    parser.add_argument("--config-root", default="docker/configs")
    parser.add_argument("--local-base-url", default=os.getenv("AICLIENT_LOCAL_BASE_URL", "http://localhost:3001"))
    parser.add_argument("--admin-password", default=os.getenv("AICLIENT_ADMIN_PASSWORD", "admin123"))
    parser.add_argument("--check-model", default=DEFAULT_CHECK_MODEL)
    parser.add_argument("--custom-name", default=None)
    parser.add_argument("--force-add", action="store_true", help="Add even if the same profileArn/refreshToken already exists.")
    parser.add_argument("--skip-local-health-check", action="store_true")
    parser.add_argument("--sync-remote", action="store_true")
    parser.add_argument("--remote-host", default=None)
    parser.add_argument("--remote-user", default=None)
    parser.add_argument("--remote-password", default=None)
    parser.add_argument("--remote-base-dir", default=os.getenv("AICLIENT_REMOTE_BASE_DIR", "/root/aiclient2api"))
    parser.add_argument("--remote-restart", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--remote-health-wait", type=int, default=8)
    parser.add_argument("--remote-api-base", default=os.getenv("AICLIENT_REMOTE_API_BASE", "https://aiclient2api.llmhub.ltd"))
    parser.add_argument("--api-key", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.token_file = str(Path(args.token_file).expanduser().resolve())
    token_path = Path(args.token_file)

    if args.wait_new_token:
        wait_for_new_token(token_path, args.wait_new_token)

    if args.watch_count > 0:
        initial_identity = None
        if token_path.exists():
            initial_identity = normalize_token_identity(validate_kiro_token(token_path))
        watch_and_sync(args, initial_identity)
    else:
        token = validate_kiro_token(token_path)
        sync_one_token(args, token)

    print("Done.")


if __name__ == "__main__":
    main()

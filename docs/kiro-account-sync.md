# Kiro Account Sync Automation

This helper imports the current local Kiro OAuth token into AIClient2API, checks
that the node is usable, and can sync the same node to the remote server.

## Local Only

After switching accounts in the Kiro client:

```powershell
.\scripts\sync-kiro-account.ps1
```

The script reads:

```text
C:\Users\<you>\.aws\sso\cache\kiro-auth-token.json
```

It then adds a `claude-kiro-oauth` node to local AIClient2API and runs a health
check against `http://localhost:3001`.

## Sync To Remote

Set remote credentials in the current PowerShell session:

```powershell
$env:AICLIENT_REMOTE_HOST = "154.9.232.80"
$env:AICLIENT_REMOTE_USER = "root"
$env:AICLIENT_REMOTE_PASSWORD = "<server-password>"
$env:AICLIENT_API_KEY = "<aiclient-api-key>"
.\scripts\sync-kiro-account.ps1 -SyncRemote
```

The script appends the node to:

```text
/root/aiclient2api/configs/provider_pools.json
```

It creates a backup before editing, uploads the token file, recreates the Docker
service, waits briefly for health, and verifies the public models endpoint when
`AICLIENT_API_KEY` is set.

## Wait While Switching Account

You can start the script first, then log in to another Kiro account:

```powershell
.\scripts\sync-kiro-account.ps1 -WaitNewTokenSeconds 300 -SyncRemote
```

It waits until the local Kiro token file changes, then imports and syncs it.

## Continuous Multi-Account Sync

The script cannot register third-party accounts for you. It can, however, watch
for manually completed Kiro logins and sync each new token automatically.

Example: sync 5 newly logged-in Kiro accounts to the remote server:

```powershell
.\scripts\sync-kiro-account.ps1 -WatchCount 5 -SyncRemote
```

Workflow:

1. Start the command.
2. Log out/in to a different Kiro account in the Kiro client.
3. Wait until the script imports, health-checks, uploads, and restarts remote.
4. Repeat until the target count is reached.

You can adjust the wait timeout per account:

```powershell
.\scripts\sync-kiro-account.ps1 -WatchCount 5 -WatchTimeoutSeconds 900 -SyncRemote
```

## Notes

- The script does not store server passwords or API keys in the repo.
- Duplicate `profileArn + refreshToken` values are detected and reused by default.
- Use `-ForceAdd` only when you intentionally want to add the same token again.

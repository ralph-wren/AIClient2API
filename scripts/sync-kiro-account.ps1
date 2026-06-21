param(
    [switch]$SyncRemote,
    [int]$WaitNewTokenSeconds = 0,
    [int]$WatchCount = 0,
    [int]$WatchTimeoutSeconds = 600,
    [string]$CustomName = "",
    [switch]$ForceAdd
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")

$argsList = @(
    (Join-Path $scriptDir "sync-kiro-account.py")
)

if ($SyncRemote) {
    $argsList += "--sync-remote"
}

if ($WaitNewTokenSeconds -gt 0) {
    $argsList += "--wait-new-token"
    $argsList += "$WaitNewTokenSeconds"
}

if ($WatchCount -gt 0) {
    $argsList += "--watch-count"
    $argsList += "$WatchCount"
    $argsList += "--watch-timeout"
    $argsList += "$WatchTimeoutSeconds"
}

if ($CustomName) {
    $argsList += "--custom-name"
    $argsList += $CustomName
}

if ($ForceAdd) {
    $argsList += "--force-add"
}

Push-Location $repoRoot
try {
    python @argsList
}
finally {
    Pop-Location
}

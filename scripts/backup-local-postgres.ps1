<#
.SYNOPSIS
    Safe break-glass backup and restore for local Postgres database.
.DESCRIPTION
    Avoids PowerShell shell redirection pitfalls (UTF-16 LE BOM encoding corruption)
    by delegating to scripts/backup_local_postgres.py or executing inside the container
    with -f /tmp/... and docker cp.
#>
[CmdletBinding()]
param(
    [ValidateSet('backup', 'restore')]
    [string]$Action = 'backup',

    [string]$Container = 'blessing-postgres-local',

    [string]$User = $env:POSTGRES_USER,

    [string]$Database = $env:POSTGRES_DB,

    [string]$File = 'break-glass-backup.sql',

    [string[]]$Tables = @('mainnet_launch_sessions', 'binance_algo_protections')
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyScript = Join-Path $scriptDir "backup_local_postgres.py"

$pythonCmd = Get-Command py -ErrorAction SilentlyContinue
if ($pythonCmd) {
    $pyExe = "py"
    $pyArgs = @("-3.13", $pyScript, "--action", $Action, "--file", $File, "--container", $Container)
} else {
    $pyExe = "python"
    $pyArgs = @($pyScript, "--action", $Action, "--file", $File, "--container", $Container)
}

if ($User) {
    $pyArgs += @("--user", $User)
}
if ($Database) {
    $pyArgs += @("--db", $Database)
}
if ($Action -eq 'backup' -and $Tables) {
    $pyArgs += @("--tables")
    $pyArgs += $Tables
}

& $pyExe @pyArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

# The Worker image is built from this working tree, so it is bound to one reviewed commit.
# A dirty or unreadable tree is refused before any Docker, Postgres, or Worker step runs.
$gitCommand = Get-Command git.exe -ErrorAction Stop
$launchHeadSha = (& $gitCommand.Source rev-parse --verify HEAD 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $launchHeadSha -notmatch '^[0-9a-f]{40}$') {
    throw "The git HEAD of this checkout cannot be read; start-local refuses to build or start anything."
}
$launchDirtyEntries = (& $gitCommand.Source status --porcelain --untracked-files=all 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "git status cannot be read; start-local refuses to build or start anything." }
if ($launchDirtyEntries) {
    throw "The working tree is not clean; start-local refuses to build the Worker image from unreviewed files. Commit or remove these entries, then rerun:`n$launchDirtyEntries"
}

function Get-LocalConfigValue([string]$Name, [string]$Default = "") {
    $processValue = [Environment]::GetEnvironmentVariable($Name, "Process")
    if (-not [string]::IsNullOrWhiteSpace($processValue)) { return $processValue.Trim() }
    $found = $null
    foreach ($fileName in @(".env", ".env.local")) {
        $filePath = Join-Path $repoRoot $fileName
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) { continue }
        foreach ($line in [System.IO.File]::ReadLines($filePath)) {
            if ($line -match ("^\s*" + [regex]::Escape($Name) + "\s*=\s*(.*?)\s*$")) {
                $value = $matches[1].Trim()
                if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                    $value = $value.Substring(1, $value.Length - 2)
                } else {
                    $value = ($value -split "\s+#", 2)[0].Trim()
                }
                $found = $value
            }
        }
    }
    if ($null -ne $found) { return $found }
    return $Default
}

function New-RandomToken {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
        return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    } finally {
        $rng.Dispose()
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Get-LocalPostgresPassword {
    $secretDirectory = Join-Path $repoRoot ".local-secrets"
    $secretPath = Join-Path $secretDirectory "postgres-password.dpapi"
    if (-not (Test-Path -LiteralPath $secretDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
    }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $secretDirectory /inheritance:r /grant:r ("{0}:(OI)(CI)F" -f $identity) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect the local Postgres credential directory." }
    Add-Type -AssemblyName System.Security -ErrorAction Stop
    $scope = [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    $password = ""
    $protectedBytes = $null
    $plainBytes = $null

    if (Test-Path -LiteralPath $secretPath -PathType Leaf) {
        $encrypted = [System.IO.File]::ReadAllText($secretPath).Trim()
        if ([string]::IsNullOrWhiteSpace($encrypted)) {
            throw "The protected local Postgres credential is empty; refusing to reset it."
        }
        try {
            if ($encrypted.StartsWith("DPAPI1:")) {
                $protectedBytes = [Convert]::FromBase64String($encrypted.Substring(7))
            } elseif ($encrypted -match "^(?:[0-9A-Fa-f]{2})+$") {
                $protectedBytes = New-Object byte[] ($encrypted.Length / 2)
                for ($index = 0; $index -lt $protectedBytes.Length; $index++) {
                    $protectedBytes[$index] = [Convert]::ToByte($encrypted.Substring($index * 2, 2), 16)
                }
            } else {
                throw "Unsupported protected credential format."
            }
            $plainBytes = [System.Security.Cryptography.ProtectedData]::Unprotect($protectedBytes, $null, $scope)
            if ($encrypted.StartsWith("DPAPI1:")) {
                $password = [System.Text.Encoding]::UTF8.GetString($plainBytes)
            } elseif ([Array]::IndexOf($plainBytes, [byte]0) -ge 0) {
                $password = [System.Text.Encoding]::Unicode.GetString($plainBytes)
            } else {
                $password = [System.Text.Encoding]::UTF8.GetString($plainBytes)
            }
        } catch {
            throw "The protected local Postgres credential cannot be decrypted by this Windows account."
        } finally {
            if ($plainBytes) { [Array]::Clear($plainBytes, 0, $plainBytes.Length) }
            if ($protectedBytes) { [Array]::Clear($protectedBytes, 0, $protectedBytes.Length) }
        }
        if ([string]::IsNullOrWhiteSpace($password)) { throw "The protected local Postgres credential decrypted to an empty value." }
        return $password
    } else {
        $bytes = New-Object byte[] 48
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $rng.GetBytes($bytes)
            $password = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
        } finally {
            $rng.Dispose()
            [Array]::Clear($bytes, 0, $bytes.Length)
        }
        try {
            $plainBytes = [System.Text.Encoding]::UTF8.GetBytes($password)
            $protectedBytes = [System.Security.Cryptography.ProtectedData]::Protect($plainBytes, $null, $scope)
            $encrypted = "DPAPI1:" + [Convert]::ToBase64String($protectedBytes)
            [System.IO.File]::WriteAllText($secretPath, $encrypted, [System.Text.Encoding]::ASCII)
            & icacls.exe $secretPath /inheritance:r /grant:r ("{0}:F" -f $identity) | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not protect the local Postgres credential file." }
        } finally {
            if ($plainBytes) { [Array]::Clear($plainBytes, 0, $plainBytes.Length) }
            if ($protectedBytes) { [Array]::Clear($protectedBytes, 0, $protectedBytes.Length) }
        }
    }
    return $password
}

function Get-PortListeners([int]$Port) {
    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
        throw "Cannot verify port $Port ownership on this Windows host; startup aborted."
    }
    try {
        $allListeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop)
        return @($allListeners | Where-Object { $_.LocalPort -eq $Port })
    } catch {
        throw "Could not verify whether port $Port is available; startup aborted."
    }
}

function Assert-RequiredPortsAvailable([int[]]$Ports = @(3001, 8000, 5433)) {
    foreach ($port in $Ports) {
        $listeners = @(Get-PortListeners $port)
        if ($listeners.Count -gt 0) {
            $owners = @($listeners | ForEach-Object { "PID $($_.OwningProcess) at $($_.LocalAddress)" }) -join ", "
            throw "Port $port is already in use ($owners). Nothing was stopped or moved."
        }
    }
}

function Assert-LocalDatabasePortMapping {
    try {
        $mapping = & $docker.Source --context $script:dockerContext port blessing-postgres-local 5432/tcp 2>$null
        $exitCode = $LASTEXITCODE
    } catch {
        $mapping = @()
        $exitCode = 1
    }
    $bindings = @($mapping | ForEach-Object { "$($_)".Trim() } | Where-Object { $_ })
    if ($exitCode -ne 0 -or $bindings.Count -ne 1 -or $bindings[0] -ne "127.0.0.1:5433") {
        throw "Local Postgres did not verify as an exclusive 127.0.0.1:5433 binding. Worker was not started."
    }
}

function Test-ManagedLocalPostgresBinding {
    try {
        $labelsJson = & $docker.Source --context $script:dockerContext inspect --format "{{json .Config.Labels}}" blessing-postgres-local 2>$null
        $labelsExitCode = $LASTEXITCODE
        $state = & $docker.Source --context $script:dockerContext inspect --format "{{.State.Status}}" blessing-postgres-local 2>$null
        $stateExitCode = $LASTEXITCODE
        $image = & $docker.Source --context $script:dockerContext inspect --format "{{.Config.Image}}" blessing-postgres-local 2>$null
        $imageExitCode = $LASTEXITCODE
        $mountsJson = & $docker.Source --context $script:dockerContext inspect --format "{{json .Mounts}}" blessing-postgres-local 2>$null
        $mountsExitCode = $LASTEXITCODE
    } catch {
        return $false
    }
    if ($labelsExitCode -ne 0 -or
        $stateExitCode -ne 0 -or "$state".Trim() -ne "running" -or
        $imageExitCode -ne 0 -or "$image".Trim() -ne "postgres:17-alpine" -or
        $mountsExitCode -ne 0) {
        return $false
    }
    try {
        $labels = "$labelsJson" | ConvertFrom-Json -ErrorAction Stop
        $mountsEnvelopeJson = '{"items":' + "$mountsJson" + '}'
        $mountsEnvelope = "$mountsEnvelopeJson" | ConvertFrom-Json -ErrorAction Stop
        $mounts = @($mountsEnvelope.items)
        $projectName = (Split-Path -Leaf $repoRoot).ToLowerInvariant() -replace "[^a-z0-9_-]", ""
        $expectedWorkingDirectory = [System.IO.Path]::GetFullPath($repoRoot)
        $expectedComposeFile = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "docker-compose.yml"))
        $expectedVolumeName = "{0}_postgres_local_data" -f $projectName
        if ("$($labels.'com.docker.compose.service')" -ne "postgres-local" -or
            "$($labels.'com.docker.compose.project')" -ne $projectName -or
            -not [string]::Equals("$($labels.'com.docker.compose.project.working_dir')", $expectedWorkingDirectory, [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals("$($labels.'com.docker.compose.project.config_files')", $expectedComposeFile, [StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
        $dataMounts = @($mounts | Where-Object { $_.Destination -eq "/var/lib/postgresql/data" })
        if ($dataMounts.Count -ne 1 -or $dataMounts[0].Type -ne "volume" -or $dataMounts[0].Name -ne $expectedVolumeName) {
            return $false
        }
        $volumeLabelsJson = & $docker.Source --context $script:dockerContext volume inspect --format "{{json .Labels}}" $expectedVolumeName 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $volumeLabels = "$volumeLabelsJson" | ConvertFrom-Json -ErrorAction Stop
        if ("$($volumeLabels.'com.docker.compose.project')" -ne $projectName -or
            "$($volumeLabels.'com.docker.compose.volume')" -ne "postgres_local_data") {
            return $false
        }
        Assert-LocalDatabasePortMapping
        return $true
    } catch {
        return $false
    }
}

function Show-UnmanagedPortStatus([int]$Port) {
    $listeners = @(Get-PortListeners $Port)
    if ($listeners.Count -eq 0) {
        Write-Host "Unmanaged port $Port is free; this launcher will not use it."
        return
    }
    $owners = @($listeners | ForEach-Object { "PID $($_.OwningProcess) at $($_.LocalAddress)" }) -join ", "
    Write-Host "Unmanaged port $Port is already in use ($owners); it will not be stopped, moved, or reused." -ForegroundColor Yellow
}

function Get-LocalDockerContext {
    try {
        $context = (& $docker.Source context show 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($context)) { return "" }
        $endpoint = (& $docker.Source context inspect --format "{{.Endpoints.docker.Host}}" $context 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { return "" }
        $allowedEndpoints = @(
            "npipe:////./pipe/docker_engine",
            "npipe:////./pipe/dockerDesktopLinuxEngine"
        )
        if ($endpoint -notin $allowedEndpoints) {
            throw "Local startup requires a verified Docker Desktop named-pipe endpoint; remote Docker contexts are not allowed."
        }
        return $context
    } catch {
        if ($_.Exception.Message -like "Local startup requires*") { throw }
        return ""
    }
}

function Get-DockerEngineVersion {
    if ([string]::IsNullOrWhiteSpace($script:dockerContext)) { return "" }
    try {
        $output = & $docker.Source --context $script:dockerContext info --format "{{.ServerVersion}}" 2>$null
        if ($LASTEXITCODE -ne 0) { return "" }
        return "$output".Trim()
    } catch {
        return ""
    }
}

Assert-RequiredPortsAvailable -Ports @(3001, 8000)
Show-UnmanagedPortStatus 8888
$docker = Get-Command docker.exe -ErrorAction Stop
$script:dockerContext = Get-LocalDockerContext
if ([string]::IsNullOrWhiteSpace($script:dockerContext)) {
    throw "A local Docker Desktop context could not be verified; no Docker command was run."
}
$python = Get-Command python.exe -ErrorAction Stop
$npm = Get-Command npm.cmd -ErrorAction Stop
# Compute the source binding once with the same read-only helper the pilot uses. It refuses a dirty tree
# and CRLF in bound files; its gitSha must be the HEAD read above, or nothing is built.
$launchBindingJson = & $python.Source -c "import json; from pathlib import Path; from scripts.local_pilot_track_c_source import source_binding; print(json.dumps(source_binding(Path('.').resolve())))"
if ($LASTEXITCODE -ne 0) { throw "The source binding for this commit could not be computed; start-local refuses to build or start anything." }
$launchBinding = "$launchBindingJson" | ConvertFrom-Json -ErrorAction Stop
if ([string]$launchBinding.gitSha -ne $launchHeadSha -or [string]$launchBinding.sourceSha256 -notmatch '^[0-9a-f]{64}$') {
    throw "The source binding does not match the checked-out HEAD; start-local refuses to build or start anything."
}
$database = Get-LocalConfigValue "POSTGRES_DB" "blessing_trading"
$databaseUser = Get-LocalConfigValue "POSTGRES_USER" "blessing_worker"
if ($database -notmatch "^[A-Za-z0-9_-]+$" -or $databaseUser -notmatch "^[A-Za-z0-9_-]+$") {
    throw "Local Postgres database/user names must contain only letters, numbers, underscore, or hyphen."
}

$dockerVersion = Get-DockerEngineVersion
if ([string]::IsNullOrWhiteSpace($dockerVersion)) {
    $service = Get-Service -Name "com.docker.service" -ErrorAction SilentlyContinue
    if ($service -and $service.Status -ne "Running") {
        try { Start-Service -Name "com.docker.service" -ErrorAction Stop } catch {
            throw "This account cannot start the Docker Desktop service. Start Docker Desktop with service permissions, then rerun; no database data was changed."
        }
    }
    $desktopPath = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (@(Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue).Count -eq 0 -and (Test-Path -LiteralPath $desktopPath -PathType Leaf)) {
        Start-Process -FilePath $desktopPath -WindowStyle Hidden
    }
    $deadline = (Get-Date).AddSeconds(90)
    do {
        Start-Sleep -Seconds 2
        $dockerVersion = Get-DockerEngineVersion
    } while ([string]::IsNullOrWhiteSpace($dockerVersion) -and (Get-Date) -lt $deadline)
}
if ([string]::IsNullOrWhiteSpace($dockerVersion)) {
    throw "Docker Engine is unavailable. Start Docker Desktop and rerun; no database data was changed."
}

$databasePortListeners = @(Get-PortListeners 5433)
$reuseExistingLocalPostgres = $false
if ($databasePortListeners.Count -gt 0) {
    if (Test-ManagedLocalPostgresBinding) {
        $reuseExistingLocalPostgres = $true
    } else {
        $owners = @($databasePortListeners | ForEach-Object { "PID $($_.OwningProcess) at $($_.LocalAddress)" }) -join ", "
        throw "Port 5433 is occupied by an unverified service ($owners). Nothing was stopped or moved."
    }
}

Assert-RequiredPortsAvailable -Ports @(3001, 8000)
$env:POSTGRES_DB = $database
$env:POSTGRES_USER = $databaseUser
$env:POSTGRES_PORT = "5433"
$env:POSTGRES_HOST = "127.0.0.1"
$postgresPassword = Get-LocalPostgresPassword
$env:POSTGRES_PASSWORD = $postgresPassword
$env:DATABASE_URL = ""

try {
    if ($reuseExistingLocalPostgres) {
        Write-Host "Reusing the verified local Postgres 17 container on 127.0.0.1:5433."
    } else {
        Write-Host "Starting only the local Postgres 17 service on 127.0.0.1:5433..."
        try {
            $composeOutput = & $docker.Source --context $script:dockerContext compose --profile local up -d postgres-local 2>&1
            $composeExitCode = $LASTEXITCODE
        } catch {
            $composeOutput = @()
            $composeExitCode = 1
        }
        if ($composeExitCode -ne 0) {
            if (-not (Test-ManagedLocalPostgresBinding)) {
                throw "Postgres did not start. Existing Docker volumes were left untouched."
            }
            Write-Warning "Docker Compose returned a non-zero status, but the expected local Postgres container and loopback port binding are verified; health, migration, and authentication checks will decide whether startup can continue."
        } else {
            Write-Host "Local Postgres container started."
        }
    }
} finally {
    $env:POSTGRES_PASSWORD = ""
}

$healthy = $false
$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Seconds 2
    try {
        $health = & $docker.Source --context $script:dockerContext inspect --format "{{.State.Health.Status}}" blessing-postgres-local 2>$null
        $healthExitCode = $LASTEXITCODE
    } catch {
        $health = ""
        $healthExitCode = 1
    }
    if ($healthExitCode -eq 0 -and "$health".Trim() -eq "healthy") { $healthy = $true }
} while (-not $healthy -and (Get-Date) -lt $deadline)
if (-not $healthy) { throw "Local Postgres did not become healthy; it was not reset or removed." }
Assert-LocalDatabasePortMapping

try {
    $env:POSTGRES_PASSWORD = $postgresPassword
    & $python.Source (Join-Path $PSScriptRoot "apply_local_postgres_migrations.py")
    if ($LASTEXITCODE -ne 0) { throw "Local Postgres migrations failed; Worker was not started." }
} finally {
    $env:POSTGRES_PASSWORD = ""
}

Assert-RequiredPortsAvailable -Ports @(3001, 8000)
$workerImageTag = "blessing-worker:local-runtime"
$workerCommitTag = "blessing-worker:" + $launchHeadSha.Substring(0, 12)
Write-Host "Building the pinned local Worker image from commit $launchHeadSha..."
& $docker.Source --context $script:dockerContext build --file Dockerfile.worker --tag $workerImageTag --tag $workerCommitTag --label "org.blessing.git.sha=$launchHeadSha" --label "org.blessing.source.sha256=$($launchBinding.sourceSha256)" $repoRoot
if ($LASTEXITCODE -ne 0) { throw "The local Worker image could not be built; the control plane was not started." }

# Fail closed: the image ID that will be pinned is read first, and its labels are checked by that ID, so a tag
# moved after this point cannot change what was verified. The commit tag must resolve to the same image.
$workerImageId = & $docker.Source --context $script:dockerContext image inspect --format "{{.Id}}" $workerImageTag
if ($LASTEXITCODE -ne 0 -or "$workerImageId".Trim() -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "The local Worker image identity could not be verified; the control plane was not started."
}
$workerImageId = "$workerImageId".Trim()
$commitTagImageId = (& $docker.Source --context $script:dockerContext image inspect --format "{{.Id}}" $workerCommitTag 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $commitTagImageId -ne $workerImageId) {
    throw "The local Worker commit tag does not resolve to the verified image; the control plane was not started."
}
# The image must carry the exact HEAD and source hash reviewed above, and the tree must still be clean and at
# that HEAD (a change made during the build would make the image unreviewed).
$builtLabelsJson = & $docker.Source --context $script:dockerContext image inspect --format "{{json .Config.Labels}}" $workerImageId 2>$null
if ($LASTEXITCODE -ne 0) { throw "The local Worker image labels could not be read; the control plane was not started." }
$builtLabelTable = @{}
try {
    $builtLabelObject = "$builtLabelsJson" | ConvertFrom-Json -ErrorAction Stop
    foreach ($property in @($builtLabelObject.PSObject.Properties)) { $builtLabelTable[$property.Name] = [string]$property.Value }
} catch { }
$currentHeadSha = (& $gitCommand.Source rev-parse --verify HEAD 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "The current git HEAD could not be read; the control plane was not started." }
$currentDirtyEntries = (& $gitCommand.Source status --porcelain --untracked-files=all 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "git status could not be read after the build; the control plane was not started." }
if ($currentDirtyEntries -or $currentHeadSha -ne $launchHeadSha -or
    $builtLabelTable['org.blessing.git.sha'] -ne $launchHeadSha -or
    $builtLabelTable['org.blessing.source.sha256'] -ne [string]$launchBinding.sourceSha256) {
    throw "The local Worker image is not bound to the current clean HEAD ($launchHeadSha); the control plane was not started."
}

$env:LOCAL_ONLY = "true"
$env:LOCAL_RUNTIME_TARGET = "LOCAL"
$env:LOCAL_RUN_ID = "run-" + [guid]::NewGuid().ToString()
$env:LOCAL_WORKER_SUPERVISOR_ENABLED = "true"
$env:LOCAL_WORKER_RUNTIME = "DOCKER"
$env:LOCAL_WORKER_IMAGE_ID = "$($workerImageId.ToString().Trim())"
$env:BIND_HOST = "127.0.0.1"
$env:LOCAL_WORKER_AUTH_REQUIRED = "true"
$env:WORKER_IDENTITY_TOKEN = New-RandomToken
$env:PORT = "8000"
$env:WORKER_URL = "http://127.0.0.1:8000"
$env:CONTROL_PLANE_URL = "http://127.0.0.1:3001"
$env:LOCAL_MAINNET_API_KEY_VERSION = Get-LocalConfigValue "BINANCE_MAINNET_API_KEY_VERSION"
$env:LOCAL_MAINNET_API_SECRET_VERSION = Get-LocalConfigValue "BINANCE_MAINNET_API_SECRET_VERSION"
$env:LOCAL_SECRET_MANAGER_PROJECT_ID = Get-LocalConfigValue "SECRET_MANAGER_PROJECT_ID" (Get-LocalConfigValue "GCP_PROJECT_ID" "gen-lang-client-0730128480")
$rawPm = (Get-LocalConfigValue "BINANCE_PORTFOLIO_MARGIN" "").Trim().ToLower()
if ($rawPm -ne "true" -and $rawPm -ne "false") {
    throw "BINANCE_PORTFOLIO_MARGIN must be set to 'true' or 'false' in .env or environment (found: '$rawPm')."
}
$env:BINANCE_PORTFOLIO_MARGIN = $rawPm
$env:EXECUTION_MODE = "PAPER"
$env:MAINNET_LIVE_APPROVED = "false"
$env:MAINNET_RELEASE_APPROVAL_ID = ""
$env:MAINNET_CONTINUATION_APPROVAL_ID = ""
$env:PERSISTENCE_MODE = "REQUIRED"
$env:EXECUTION_LEASE_REQUIRED = "true"
$env:BINANCE_API_KEY = ""
$env:BINANCE_API_SECRET = ""
$env:BINANCE_TESTNET_API_KEY = ""
$env:BINANCE_TESTNET_API_SECRET = ""
$env:BINANCE_MAINNET_API_KEY = ""
$env:BINANCE_MAINNET_API_SECRET = ""
$env:BINANCE_MAINNET_API_KEY_VERSION = ""
$env:BINANCE_MAINNET_API_SECRET_VERSION = ""
$env:NODE_ENV = "development"

try {
    Write-Host "Starting the Local supervisor on loopback with REQUIRED Postgres persistence."
    Write-Host "The supervisor starts PAPER/DISARMED and cannot access Mainnet secrets without a one-time Firebase trading_admin approval."
    Write-Host "Open http://127.0.0.1:3001 in this computer's browser after connecting with Chrome Remote Desktop."
    Assert-RequiredPortsAvailable -Ports @(3001)
    $env:PORT = "3001"
    $env:POSTGRES_PASSWORD = $postgresPassword
    & $npm.Source run dev
} finally {
    $postgresPassword = ""
    $env:BINANCE_MAINNET_API_KEY = ""
    $env:BINANCE_MAINNET_API_SECRET = ""
    $env:BINANCE_MAINNET_API_KEY_VERSION = ""
    $env:BINANCE_MAINNET_API_SECRET_VERSION = ""
    $env:POSTGRES_PASSWORD = ""
    $env:WORKER_IDENTITY_TOKEN = ""
}

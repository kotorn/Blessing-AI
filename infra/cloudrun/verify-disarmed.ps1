param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Region = "asia-southeast1",
  [string]$ServiceName = "blessing-trading-worker",
  [string]$ExpectedImageDigest = "",
  [string]$RuntimeServiceAccount = "blessing-runtime@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string]$ExpectedExecutionMode = "PAPER"
)

$ErrorActionPreference = "Stop"

if ($RuntimeServiceAccount -notmatch '^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$') {
  throw "RuntimeServiceAccount must be a service-account email"
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedImageDigest) -and $ExpectedImageDigest -notmatch '@sha256:[0-9a-fA-F]{64}$') {
  throw "ExpectedImageDigest must be an immutable registry digest"
}

$serviceJson = & gcloud run services describe $ServiceName `
  --project=$ProjectId `
  --region=$Region `
  --format=json
if ($LASTEXITCODE -ne 0) {
  throw "Unable to read Cloud Run service"
}
try {
  $service = $serviceJson | ConvertFrom-Json
} catch {
  throw "Cloud Run service description is not valid JSON"
}

$latestReadyRevision = [string]$service.status.latestReadyRevisionName
$readyCondition = @($service.status.conditions) |
  Where-Object { $_.type -eq "Ready" } |
  Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($latestReadyRevision) -or $null -eq $readyCondition -or $readyCondition.status -ne "True") {
  throw "Cloud Run service has no Ready latest revision"
}

$container = @($service.spec.template.spec.containers) | Select-Object -First 1
if ($null -eq $container) {
  throw "Cloud Run service has no container"
}
$image = [string]$container.image
if ($image -notmatch '@sha256:[0-9a-fA-F]{64}$') {
  throw "Cloud Run revision image is not pinned to an immutable digest"
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedImageDigest) -and $image -ne $ExpectedImageDigest) {
  throw "Cloud Run revision image digest does not match ExpectedImageDigest"
}

function Get-EnvEntry {
  param([string]$Name)
  return @($container.env) | Where-Object { [string]$_.name -eq $Name } | Select-Object -First 1
}

function Get-PlainEnvValue {
  param([string]$Name)
  $entry = Get-EnvEntry $Name
  if ($null -eq $entry -or $null -eq $entry.value) {
    throw "Required plain environment value is missing: $Name"
  }
  return [string]$entry.value
}

if ((Get-PlainEnvValue "EXECUTION_MODE") -ne $ExpectedExecutionMode) {
  throw "Default Cloud Run revision must remain $ExpectedExecutionMode"
}
if ((Get-PlainEnvValue "PERSISTENCE_MODE") -ne "REQUIRED") {
  throw "Cloud Run Worker must use REQUIRED persistence"
}
if ((Get-PlainEnvValue "MAINNET_LIVE_APPROVED").ToLowerInvariant() -ne "false") {
  throw "Default Cloud Run revision must keep MAINNET_LIVE_APPROVED=false"
}
if ([string]$service.spec.template.spec.serviceAccountName -ne $RuntimeServiceAccount) {
  throw "Cloud Run runtime service account does not match the expected account"
}

foreach ($name in @("POSTGRES_PASSWORD", "BINANCE_MAINNET_API_KEY", "BINANCE_MAINNET_API_SECRET")) {
  $entry = Get-EnvEntry $name
  $secretRef = $entry.valueFrom.secretKeyRef
  if ($null -eq $secretRef -or [string]::IsNullOrWhiteSpace([string]$secretRef.name)) {
    throw "$name must be injected from Secret Manager"
  }
  if ([string]$secretRef.key -notmatch '^[1-9][0-9]*$') {
    throw "$name must pin a numeric Secret Manager version"
  }
}

$traffic = @($service.status.traffic) |
  Where-Object { $_.revisionName -eq $latestReadyRevision -and [int]$_.percent -gt 0 }
if ($traffic.Count -eq 0) {
  throw "Latest ready revision has no positive traffic"
}

Write-Output "Cloud Run disarmed revision verified: $latestReadyRevision"
Write-Output "Image digest verified: $image"
Write-Output "Execution mode verified: $ExpectedExecutionMode; Mainnet approval verified: false"
Write-Output "Secret references verified: pinned numeric versions"

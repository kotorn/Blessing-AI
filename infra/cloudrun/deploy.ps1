param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Region = "asia-southeast1",
  [string]$ImageUri = "",
  [switch]$LiveRelease
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ImageUri)) {
  throw "Pass an immutable ImageUri. This script never builds or selects a live image implicitly."
}

if ($LiveRelease) {
  throw "Live release is a separate controlled action. Deploy the disarmed revision first, then use the reviewed release runbook to set MAINNET_LIVE_APPROVED=true."
}

function Invoke-GCloud {
  param([Parameter(Mandatory = $true)][string[]]$Arguments)

  & gcloud @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "gcloud command failed with exit code ${LASTEXITCODE}: gcloud $($Arguments -join ' ')"
  }
}

Invoke-GCloud @("config", "set", "project", $ProjectId)
Invoke-GCloud @(
  "run", "deploy", "blessing-trading-worker",
  "--image", $ImageUri,
  "--region", $Region,
  "--platform", "managed",
  "--service-account", "blessing-runtime@${ProjectId}.iam.gserviceaccount.com",
  "--min", "1",
  "--max", "1",
  "--concurrency", "1",
  "--cpu", "1",
  "--memory", "1Gi",
  "--no-cpu-throttling",
  "--add-cloudsql-instances", "${ProjectId}:${Region}:blessing-sql-primary",
  "--set-env-vars", "EXECUTION_MODE=PAPER,PERSISTENCE_MODE=REQUIRED,EXECUTION_LEASE_REQUIRED=true,MAINNET_LIVE_APPROVED=false,POSTGRES_HOST=/cloudsql/${ProjectId}:${Region}:blessing-sql-primary,POSTGRES_PORT=5432,POSTGRES_DB=blessing_trading,POSTGRES_USER=blessing_worker",
  "--set-secrets", "POSTGRES_PASSWORD=blessing-cloud-sql-password:latest,BINANCE_MAINNET_API_KEY=blessing-binance-mainnet-api-key:latest,BINANCE_MAINNET_API_SECRET=blessing-binance-mainnet-api-secret:latest",
  "--no-allow-unauthenticated"
)

$serviceJson = & gcloud run services describe blessing-trading-worker --region $Region --format=json
if ($LASTEXITCODE -ne 0) {
  throw "gcloud service readiness inspection failed with exit code $LASTEXITCODE"
}
try {
  $service = $serviceJson | ConvertFrom-Json
} catch {
  throw "Cloud Run readiness inspection returned invalid JSON"
}

$latestReadyRevision = [string]$service.status.latestReadyRevisionName
$readyCondition = @($service.status.conditions) |
  Where-Object { $_.type -eq "Ready" } |
  Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($latestReadyRevision) -or $null -eq $readyCondition -or $readyCondition.status -ne "True") {
  throw "Cloud Run service has no Ready latest revision"
}

$trafficToReady = @($service.status.traffic) |
  Where-Object { $_.revisionName -eq $latestReadyRevision } |
  Where-Object { [int]$_.percent -gt 0 }
if ($trafficToReady.Count -eq 0) {
  throw "Cloud Run latest ready revision has no positive traffic"
}

Write-Output "Cloud Run ready revision verified: $latestReadyRevision"

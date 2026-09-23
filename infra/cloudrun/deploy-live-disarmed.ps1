<#
Deploy a LIVE-configured but DISARMED Worker revision.

This is a release-controller action, not a trading action. It pins the image
and Secret Manager versions, keeps MAINNET_LIVE_APPROVED=false, and never
calls a Binance order endpoint.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Region = "asia-southeast1",
  [Parameter(Mandatory = $true)]
  [string]$ImageUri,
  [ValidatePattern('^[1-9][0-9]*$')]
  [string]$CloudSqlPasswordVersion = "1",
  [ValidatePattern('^[1-9][0-9]*$')]
  [string]$BinanceMainnetApiKeyVersion = "2",
  [ValidatePattern('^[1-9][0-9]*$')]
  [string]$BinanceMainnetApiSecretVersion = "2",
  [string]$WorkerImageDigest = "",
  [string]$WorkerRevision = ""
)

$ErrorActionPreference = "Stop"
if ($ImageUri -notmatch '@sha256:[0-9a-fA-F]{64}$') { throw "ImageUri must use an immutable digest" }
if ($WorkerImageDigest -and $WorkerImageDigest -notmatch '@sha256:[0-9a-fA-F]{64}$') { throw "WorkerImageDigest must use an immutable digest" }

function Invoke-GCloud {
  param([Parameter(Mandatory = $true)][string[]]$Arguments)
  & gcloud @Arguments
  if ($LASTEXITCODE -ne 0) { throw "gcloud command failed with exit code ${LASTEXITCODE}" }
}

$effectiveWorkerImageDigest = if ($WorkerImageDigest) { $WorkerImageDigest } else { $ImageUri }

$envVars = @(
  "EXECUTION_MODE=LIVE",
  "MAINNET_LIVE_APPROVED=false",
  "PERSISTENCE_MODE=REQUIRED",
  "EXECUTION_LEASE_REQUIRED=true",
  "MAINNET_LAUNCH_POLICY=STAGED_FIRST_ORDER",
  "MAINNET_MAX_RISK_INCREASING_ORDERS=1",
  "MAINNET_PREFLIGHT_MAX_AGE_SEC=60",
  "MAINNET_RELEASE_APPROVAL_ID=",
  "MAINNET_MAX_COLLATERAL=250",
  "MAINNET_MAX_LEVERAGE=2",
  "MAINNET_MAX_DAILY_LOSS=25",
  "MAX_DRAWDOWN_PCT=20",
  "BINANCE_PORTFOLIO_MARGIN=true",
  "WORKER_IMAGE_DIGEST=$effectiveWorkerImageDigest",
  "WORKER_REVISION=$WorkerRevision",
  "CLOUD_SQL_PASSWORD_VERSION=$CloudSqlPasswordVersion",
  "BINANCE_MAINNET_API_KEY_VERSION=$BinanceMainnetApiKeyVersion",
  "BINANCE_MAINNET_API_SECRET_VERSION=$BinanceMainnetApiSecretVersion",
  "POSTGRES_HOST=/cloudsql/${ProjectId}:${Region}:blessing-sql-primary",
  "POSTGRES_PORT=5432",
  "POSTGRES_DB=blessing_trading",
  "POSTGRES_USER=blessing_worker"
) -join ","

Invoke-GCloud @(
  "run", "deploy", "blessing-trading-worker",
  "--project=$ProjectId",
  "--region=$Region",
  "--platform=managed",
  "--image=$ImageUri",
  "--service-account=blessing-runtime@${ProjectId}.iam.gserviceaccount.com",
  "--min=1",
  "--max=1",
  "--concurrency=1",
  "--cpu=1",
  "--memory=1Gi",
  "--no-cpu-throttling",
  "--add-cloudsql-instances=${ProjectId}:${Region}:blessing-sql-primary",
  "--set-env-vars=$envVars",
  "--set-secrets=POSTGRES_PASSWORD=blessing-cloud-sql-password:${CloudSqlPasswordVersion},BINANCE_MAINNET_API_KEY=blessing-binance-mainnet-api-key:${BinanceMainnetApiKeyVersion},BINANCE_MAINNET_API_SECRET=blessing-binance-mainnet-api-secret:${BinanceMainnetApiSecretVersion}",
  "--no-allow-unauthenticated"
)

Write-Output "LIVE disarmed Worker deployment requested and pinned to $ImageUri"
Write-Output "MAINNET_LIVE_APPROVED=false; engine starts DISARMED; no order was submitted"

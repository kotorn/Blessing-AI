<#
.SYNOPSIS
  Deploy the dedicated Blessing AI Control Plane from an immutable image.

.DESCRIPTION
  The Control Plane may expose public HTTPS transport for the SPA, but its
  protected APIs enforce Firebase RBAC and its internal release APIs enforce a
  Google-signed OIDC identity. This deployment never receives Binance or SQL
  credentials and never arms the Worker.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Region = "asia-southeast1",
  [string]$ServiceName = "blessing-control-plane",
  [Parameter(Mandatory = $true)]
  [string]$ImageUri,
  [Parameter(Mandatory = $true)]
  [string]$ControlPlaneUrl,
  [Parameter(Mandatory = $true)]
  [string]$WorkerUrl,
  [string]$ControlPlaneServiceAccount = "blessing-control-plane@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string]$ReleaseControllerServiceAccount = "blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [Parameter(Mandatory = $true)]
  [string]$WorkerImageDigest,
  [Parameter(Mandatory = $true)]
  [string]$WorkerRevision,
  [ValidateSet("DEV_PAPER_UI", "MAINNET_OPERATOR_UI")]
  [string]$RuntimeProfile = "MAINNET_OPERATOR_UI"
)

$ErrorActionPreference = "Stop"

if ($ImageUri -notmatch '@sha256:[0-9a-fA-F]{64}$') {
  throw "Control Plane image must use an immutable registry digest"
}
if ($WorkerImageDigest -notmatch '@sha256:[0-9a-fA-F]{64}$') {
  throw "WorkerImageDigest must use an immutable registry digest"
}
if ($ControlPlaneUrl -notmatch '^https://[^/]+$' -or $WorkerUrl -notmatch '^https://[^/]+$') {
  throw "ControlPlaneUrl and WorkerUrl must be canonical HTTPS service URLs"
}
if ($ControlPlaneServiceAccount -notmatch '^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$') {
  throw "ControlPlaneServiceAccount must be a service-account email"
}
if ($ReleaseControllerServiceAccount -notmatch '^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$') {
  throw "ReleaseControllerServiceAccount must be a service-account email"
}

$profileSettings = switch ($RuntimeProfile) {
  "DEV_PAPER_UI" {
    @{ min = 0; max = 1 }
  }
  "MAINNET_OPERATOR_UI" {
    @{ min = 1; max = 1 }
  }
}

function Invoke-GCloud {
  param([Parameter(Mandatory = $true)][string[]]$Arguments)
  & gcloud @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "gcloud command failed with exit code ${LASTEXITCODE}"
  }
}

$envVars = @(
  "NODE_ENV=production",
  "CONTROL_PLANE_ONLY=true",
  "CONTROL_PLANE_AUTH_REQUIRED=true",
  "CONTROL_PLANE_URL=$ControlPlaneUrl",
  "CONTROL_PLANE_ALLOWED_SERVICE_ACCOUNTS=$ReleaseControllerServiceAccount",
  "RELEASE_CONTROLLER_SERVICE_ACCOUNT=$ReleaseControllerServiceAccount",
  "WORKER_URL=$WorkerUrl",
  "WORKER_IMAGE_DIGEST=$WorkerImageDigest",
  "WORKER_REVISION=$WorkerRevision",
  "VITE_DATA_CONNECT_CUTOVER=false"
) -join ","

# IAM policy changes are performed by the separately reviewed identity
# provisioning step. The Release Controller must not hold
# run.services.setIamPolicy, so deployment itself leaves transport policy
# unchanged; protected routes still require server-side Firebase claims.
Invoke-GCloud @(
  "run", "deploy", $ServiceName,
  "--project=$ProjectId",
  "--region=$Region",
  "--platform=managed",
  "--image=$ImageUri",
  "--service-account=$ControlPlaneServiceAccount",
  "--min=$($profileSettings.min)",
  "--max=$($profileSettings.max)",
  "--concurrency=1",
  "--cpu=1",
  "--memory=1Gi",
  "--cpu-throttling",
  "--set-env-vars=$envVars"
)

$serviceJson = & gcloud run services describe $ServiceName --project=$ProjectId --region=$Region --format=json
if ($LASTEXITCODE -ne 0) { throw "Control Plane read-back failed" }
$service = $serviceJson | ConvertFrom-Json
$container = @($service.spec.template.spec.containers) | Select-Object -First 1
$actualImage = [string]$container.image
$actualServiceAccount = [string]$service.spec.template.spec.serviceAccountName
$ready = @($service.status.conditions) | Where-Object { $_.type -eq "Ready" -and $_.status -eq "True" }
if ($actualImage -ne $ImageUri) { throw "Control Plane image digest read-back does not match requested digest" }
if ($actualServiceAccount -ne $ControlPlaneServiceAccount) { throw "Control Plane service account read-back does not match" }
if ($ready.Count -eq 0) { throw "Control Plane latest revision is not Ready" }

$annotations = $service.spec.template.metadata.annotations
$actualMin = if ($null -eq $annotations.'autoscaling.knative.dev/minScale') { 0 } else { [int]$annotations.'autoscaling.knative.dev/minScale' }
$actualMax = if ($null -eq $annotations.'autoscaling.knative.dev/maxScale') { 0 } else { [int]$annotations.'autoscaling.knative.dev/maxScale' }
$cpuThrottling = [string]$annotations.'run.googleapis.com/cpu-throttling'
if ($actualMin -ne [int]$profileSettings.min -or $actualMax -ne [int]$profileSettings.max) {
  throw "Control Plane runtime profile $RuntimeProfile expects min=$($profileSettings.min), max=$($profileSettings.max); got min=$actualMin, max=$actualMax"
}
if ($cpuThrottling.ToLowerInvariant() -ne "true") {
  throw "Control Plane runtime profile $RuntimeProfile requires request-based CPU throttling"
}

Write-Output "Control Plane deployed and verified: $ServiceName"
Write-Output "Immutable image verified: $actualImage"
Write-Output "Runtime profile verified: $RuntimeProfile (min=$actualMin, max=$actualMax, request-based CPU)"
Write-Output "No Binance or SQL secrets were supplied to the Control Plane deployment"

<#
.SYNOPSIS
  Bootstrap the Control Plane URL, then deploy the same immutable image again.

.DESCRIPTION
  Cloud Run assigns a service URL only after the service exists. This helper
  performs a no-traffic bootstrap revision with a non-secret placeholder,
  reads the canonical URL back, and invokes the normal immutable deployment
  helper for the final revision. IAM transport policy is intentionally not
  changed here; public SPA transport, if desired, is configured by the
  separately reviewed identity-provisioning step.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Region = "asia-southeast1",
  [string]$ServiceName = "blessing-control-plane",
  [Parameter(Mandatory = $true)]
  [string]$ImageUri,
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
if ($WorkerUrl -notmatch '^https://[^/]+$') {
  throw "WorkerUrl must be a canonical HTTPS service URL"
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

# This first revision is deliberately not routed traffic and contains no
# credential. Its only purpose is to make the service URL discoverable.
$bootstrapEnv = @(
  "NODE_ENV=production",
  "CONTROL_PLANE_ONLY=true",
  "CONTROL_PLANE_AUTH_REQUIRED=true",
  "CONTROL_PLANE_URL=https://bootstrap.invalid",
  "CONTROL_PLANE_ALLOWED_SERVICE_ACCOUNTS=$ReleaseControllerServiceAccount",
  "RELEASE_CONTROLLER_SERVICE_ACCOUNT=$ReleaseControllerServiceAccount",
  "WORKER_URL=$WorkerUrl",
  "WORKER_IMAGE_DIGEST=$WorkerImageDigest",
  "WORKER_REVISION=$WorkerRevision",
  "VITE_DATA_CONNECT_CUTOVER=false"
) -join ","

# Cloud Run rejects --no-traffic when a service does not exist yet, so the
# very first deploy of a brand-new service unavoidably takes 100% traffic
# for this bootstrap revision. That revision holds no secret and its
# CONTROL_PLANE_URL is a non-resolving placeholder, so this is safe; every
# subsequent bootstrap run (service already exists) keeps --no-traffic.
# --no-allow-unauthenticated is explicit so a first-time creation does not
# stall on gcloud's interactive IAM prompt; IAM is otherwise left to the
# separately reviewed identity-provisioning step.
& gcloud run services describe $ServiceName --project=$ProjectId --region=$Region --format="value(metadata.name)" 2>$null | Out-Null
$serviceAlreadyExists = $LASTEXITCODE -eq 0

$bootstrapDeployArgs = @(
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
  "--set-env-vars=$bootstrapEnv",
  "--no-allow-unauthenticated"
)
if ($serviceAlreadyExists) {
  $bootstrapDeployArgs += "--no-traffic"
}
Invoke-GCloud $bootstrapDeployArgs

$serviceJson = & gcloud run services describe $ServiceName `
  --project=$ProjectId `
  --region=$Region `
  --format=json
if ($LASTEXITCODE -ne 0) { throw "Control Plane bootstrap read-back failed" }
$service = $serviceJson | ConvertFrom-Json
$controlPlaneUrl = [string]$service.status.url
if ($controlPlaneUrl -notmatch '^https://[^/]+$') {
  throw "Cloud Run did not return a canonical Control Plane URL"
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $scriptDir "deploy-control-plane.ps1") `
  -ProjectId $ProjectId `
  -Region $Region `
  -ServiceName $ServiceName `
  -ImageUri $ImageUri `
  -ControlPlaneUrl $controlPlaneUrl `
  -WorkerUrl $WorkerUrl `
  -ControlPlaneServiceAccount $ControlPlaneServiceAccount `
  -ReleaseControllerServiceAccount $ReleaseControllerServiceAccount `
  -WorkerImageDigest $WorkerImageDigest `
  -WorkerRevision $WorkerRevision `
  -RuntimeProfile $RuntimeProfile
if ($LASTEXITCODE -ne 0) {
  throw "Final Control Plane deployment failed"
}

Write-Output "Control Plane two-pass deployment verified: $controlPlaneUrl ($RuntimeProfile)"
Write-Output "Bootstrap used no secrets and routed no traffic"

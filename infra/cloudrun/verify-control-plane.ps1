<#
Read back the dedicated Control Plane deployment contract.

This check does not inspect secret values. Public HTTPS transport is allowed
for the SPA, while protected API authorization is enforced in the Node server.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Region = "asia-southeast1",
  [string]$ServiceName = "blessing-control-plane",
  [Parameter(Mandatory = $true)]
  [string]$ExpectedImageDigest,
  [Parameter(Mandatory = $true)]
  [string]$ControlPlaneUrl,
  [Parameter(Mandatory = $true)]
  [string]$WorkerUrl,
  [string]$ControlPlaneServiceAccount = "blessing-control-plane@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string]$ReleaseControllerServiceAccount = "blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [ValidateSet("DEV_PAPER_UI", "MAINNET_OPERATOR_UI")]
  [string]$RuntimeProfile = "MAINNET_OPERATOR_UI"
)

$ErrorActionPreference = "Stop"
if ($ExpectedImageDigest -notmatch '@sha256:[0-9a-fA-F]{64}$') { throw "ExpectedImageDigest must be immutable" }
if ($ControlPlaneUrl -notmatch '^https://[^/]+$' -or $WorkerUrl -notmatch '^https://[^/]+$') { throw "Service URLs must be canonical HTTPS URLs" }

$json = & gcloud run services describe $ServiceName --project=$ProjectId --region=$Region --format=json
if ($LASTEXITCODE -ne 0) { throw "Unable to read Control Plane Cloud Run service" }
$service = $json | ConvertFrom-Json
$container = @($service.spec.template.spec.containers) | Select-Object -First 1
if ($null -eq $container -or [string]$container.image -ne $ExpectedImageDigest) { throw "Control Plane image digest does not match" }
if ([string]$service.spec.template.spec.serviceAccountName -ne $ControlPlaneServiceAccount) { throw "Control Plane runtime service account does not match" }

function Get-PlainEnvValue {
  param([string]$Name)
  $entry = @($container.env) | Where-Object { [string]$_.name -eq $Name } | Select-Object -First 1
  if ($null -eq $entry -or $null -eq $entry.value) { throw "Control Plane environment is missing $Name" }
  return [string]$entry.value
}

if ((Get-PlainEnvValue "CONTROL_PLANE_ONLY").ToLowerInvariant() -ne "true") { throw "CONTROL_PLANE_ONLY must be true" }
if ((Get-PlainEnvValue "CONTROL_PLANE_AUTH_REQUIRED").ToLowerInvariant() -ne "true") { throw "Control Plane auth must be required" }
if ((Get-PlainEnvValue "CONTROL_PLANE_URL").TrimEnd('/') -ne $ControlPlaneUrl.TrimEnd('/')) { throw "CONTROL_PLANE_URL does not match" }
if ((Get-PlainEnvValue "WORKER_URL").TrimEnd('/') -ne $WorkerUrl.TrimEnd('/')) { throw "WORKER_URL does not match" }
if ((Get-PlainEnvValue "RELEASE_CONTROLLER_SERVICE_ACCOUNT") -ne $ReleaseControllerServiceAccount) {
  throw "Release Controller service-account allowlist does not match"
}
if ((Get-PlainEnvValue "CONTROL_PLANE_ALLOWED_SERVICE_ACCOUNTS") -ne $ReleaseControllerServiceAccount) {
  throw "Control Plane OIDC allowlist does not match"
}
if ((Get-PlainEnvValue "VITE_DATA_CONNECT_CUTOVER").ToLowerInvariant() -ne "false") { throw "Data Connect cutover must remain false" }

# The Control Plane uses ADC and must never receive Binance, SQL, or any other
# Secret Manager value. A secret reference in its Cloud Run revision is a hard
# failure even when the environment variable name is unfamiliar.
foreach ($entry in @($container.env)) {
  if ($null -ne $entry.valueFrom -and $null -ne $entry.valueFrom.secretKeyRef) {
    throw "Control Plane revision must not contain a Secret Manager reference"
  }
  if ([string]$entry.name -match '(?i)(api[-_ ]?key|api[-_ ]?secret|password|token|dsn|private[-_ ]?key)') {
    throw "Control Plane revision contains a credential-like environment variable"
  }
}

$ready = @($service.status.conditions) | Where-Object { $_.type -eq "Ready" -and $_.status -eq "True" }
if ($ready.Count -eq 0) { throw "Control Plane latest revision is not Ready" }

$profileSettings = switch ($RuntimeProfile) {
  "DEV_PAPER_UI" { @{ min = 0; max = 1 } }
  "MAINNET_OPERATOR_UI" { @{ min = 1; max = 1 } }
}
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

Write-Output "Control Plane verified: immutable image, dedicated service account, worker URL, cutover=false, profile=$RuntimeProfile"

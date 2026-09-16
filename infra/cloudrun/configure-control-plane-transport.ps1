<#
.SYNOPSIS
  Configure the optional public HTTPS transport for the Control Plane.

.DESCRIPTION
  This is an explicit administrator IAM operation kept outside the Release
  Controller. Public transport is acceptable only because every protected API
  performs server-side Firebase authentication and RBAC. This script never
  changes the Trading Worker policy and never handles a secret.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Region = "asia-southeast1",
  [string]$ServiceName = "blessing-control-plane",
  [switch]$Apply
)

$ErrorActionPreference = "Stop"

function Invoke-GCloud {
  param([Parameter(Mandatory = $true)][string[]]$Arguments)
  & gcloud @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "gcloud command failed with exit code ${LASTEXITCODE}"
  }
}

$service = & gcloud run services describe $ServiceName `
  --project=$ProjectId `
  --region=$Region `
  --format="value(metadata.name)" 2>$null
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($service -join "").Trim())) {
  throw "Control Plane service does not exist; deploy it before configuring transport"
}

if (-not $Apply) {
  Write-Output "DRY_RUN: no IAM change made"
  Write-Output "DRY_RUN would grant allUsers roles/run.invoker on $ServiceName only"
  exit 0
}

Invoke-GCloud @(
  "run", "services", "add-iam-policy-binding", $ServiceName,
  "--project=$ProjectId",
  "--region=$Region",
  "--member=allUsers",
  "--role=roles/run.invoker",
  "--quiet"
)

$policy = & gcloud run services get-iam-policy $ServiceName `
  --project=$ProjectId `
  --region=$Region `
  --format=json
if ($LASTEXITCODE -ne 0) { throw "Control Plane transport policy read-back failed" }
$parsed = $policy | ConvertFrom-Json
$publicInvoker = @(
  foreach ($binding in @($parsed.bindings)) {
    if ([string]$binding.role -eq "roles/run.invoker") {
      @($binding.members) | ForEach-Object { [string]$_ }
    }
  }
)
if ($publicInvoker -notcontains "allUsers") {
  throw "Control Plane public transport binding was not verified"
}

Write-Output "Control Plane public HTTPS transport verified; protected routes remain server-authenticated"

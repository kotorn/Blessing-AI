<#
.SYNOPSIS
  Read back continuation readiness through the Release Controller boundary.

.DESCRIPTION
  This is a read-only verification probe. It never calls ARM, CONTINUE,
  order, cancel, transfer, leverage, or position-mode mutation routes.
#>
param(
  [Parameter(Mandatory = $true)]
  [string]$ControlPlaneUrl,
  [Parameter(Mandatory = $true)]
  [string]$LaunchId
)

$ErrorActionPreference = "Stop"

if ($ControlPlaneUrl -notmatch '^https://[^/?:#]+$') {
  throw "ControlPlaneUrl must be a canonical HTTPS service URL"
}
if ($LaunchId -notmatch '^launch-[A-Za-z0-9-]{8,127}$') {
  throw "LaunchId is invalid"
}

$token = (& gcloud auth print-identity-token --audiences=$ControlPlaneUrl 2>$null).Trim()
if ([string]::IsNullOrWhiteSpace($token)) {
  throw "Release Controller OIDC token could not be obtained"
}

$headers = @{ Authorization = "Bearer $token" }
$body = @{ launchId = $LaunchId } | ConvertTo-Json -Compress
$result = Invoke-RestMethod `
  -Method Post `
  -Uri "$($ControlPlaneUrl.TrimEnd('/'))/internal/release/continuation-readiness" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body

if ($result.evidence_status -ne "VERIFIED" -or $result.continuationReady -ne $true) {
  throw "Continuation readiness is not verified"
}
if ($result.executionMode -ne "LIVE") {
  throw "Continuation evidence is not for LIVE execution"
}
if ($result.mainnetLiveApproved -ne $true) {
  throw "Continuation evidence is not bound to an approved LIVE Worker"
}
if ($result.engineState -notin @("PAUSED_NEW_RISK", "DISARMED")) {
  throw "Worker is not paused or disarmed for continuation"
}
if ($result.launchPolicy -notin @("STAGED_FIRST_ORDER", "AUTONOMOUS_AFTER_REVIEW")) {
  throw "Continuation evidence has an unsupported launch policy"
}
if ($result.launchState -notin @("PAUSED_NEW_RISK", "REAUTH_REQUIRED")) {
  throw "Launch session is not awaiting continuation"
}
if ([int]$result.submittedOrders -lt 1) {
  throw "Continuation has no durable first-order evidence"
}
if ([int]$result.preflightOrderEndpointAttempts -ne 0 -or [int]$result.preflightOrderSubmissionAttempts -ne 0) {
  throw "Read-only continuation preflight reported an order attempt"
}
if ($result.persistenceDurable -ne $true) {
  throw "Continuation persistence is not durable"
}
$secretVersions = $result.secretVersions
if ($null -eq $secretVersions -or
    $secretVersions.sql -notmatch '^[1-9][0-9]*$' -or
    $secretVersions.apiKey -notmatch '^[1-9][0-9]*$' -or
    $secretVersions.apiSecret -notmatch '^[1-9][0-9]*$') {
  throw "Continuation evidence does not contain numeric Secret Manager versions"
}

Write-Output "Continuation readiness verified: launch=$LaunchId state=$($result.launchState)"
Write-Output "Read-only preflight evidence verified with zero order attempts"

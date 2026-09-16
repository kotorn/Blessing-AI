<#
Verify a LIVE Worker revision is still disarmed.

The script reads Cloud Run configuration and asks the authenticated Control
Plane for Worker state. It does not call Binance and does not send ARM/order
requests.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Region = "asia-southeast1",
  [string]$ServiceName = "blessing-trading-worker",
  [string]$RuntimeServiceAccount = "blessing-runtime@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [Parameter(Mandatory = $true)]
  [string]$ExpectedImageDigest,
  [Parameter(Mandatory = $true)]
  [string]$ControlPlaneUrl,
  [ValidateSet("true", "false")]
  [string]$ExpectedMainnetLiveApproved = "false"
)

$ErrorActionPreference = "Stop"
if ($ExpectedImageDigest -notmatch '@sha256:[0-9a-fA-F]{64}$') { throw "ExpectedImageDigest must be immutable" }
if ($ControlPlaneUrl -notmatch '^https://[^/]+$') { throw "ControlPlaneUrl must be canonical HTTPS" }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $scriptDir "verify-iam.ps1") -ProjectId $ProjectId -Region $Region -ServiceName $ServiceName
& (Join-Path $scriptDir "verify-disarmed.ps1") `
  -ProjectId $ProjectId `
  -Region $Region `
  -ServiceName $ServiceName `
  -RuntimeServiceAccount $RuntimeServiceAccount `
  -ExpectedImageDigest $ExpectedImageDigest `
  -ExpectedExecutionMode "LIVE" `
  -ExpectedMainnetLiveApproved $ExpectedMainnetLiveApproved

$token = (& gcloud auth print-identity-token --audiences=$ControlPlaneUrl 2>$null).Trim()
if ([string]::IsNullOrWhiteSpace($token)) { throw "Release Controller OIDC token could not be obtained" }
$headers = @{ Authorization = "Bearer $token" }
$runtime = Invoke-RestMethod -Method Post -Uri "$ControlPlaneUrl/internal/release/runtime" -Headers $headers -ContentType "application/json" -Body "{}"
if ($runtime.evidence_status -ne "VERIFIED") { throw "Control Plane runtime evidence is not verified" }
if ($runtime.state.executionMode -ne "LIVE") { throw "Worker execution mode is not LIVE" }
$expectedApproved = $ExpectedMainnetLiveApproved -eq "true"
if ($runtime.state.mainnetLiveApproved -ne $expectedApproved) {
  throw "Worker Mainnet approval flag does not match expected value $ExpectedMainnetLiveApproved"
}
if ($runtime.state.engineState -ne "DISARMED") { throw "LIVE-approved Worker did not start DISARMED" }
if ([int]$runtime.state.orderSubmissionAttempts -ne 0) { throw "Worker reports an order submission during disarmed verification" }
if ($runtime.state.privateStreamHealthy -eq $true) { throw "Private stream must remain stopped before ARM" }
if ($runtime.persistence.mode -ne "REQUIRED" -or $runtime.persistence.durable -ne $true) { throw "Worker persistence is not durable REQUIRED" }
$secretVersions = $runtime.state.secretVersions
if ($null -eq $secretVersions -or
    $secretVersions.sql -notmatch '^[1-9][0-9]*$' -or
    $secretVersions.apiKey -notmatch '^[1-9][0-9]*$' -or
    $secretVersions.apiSecret -notmatch '^[1-9][0-9]*$') {
  throw "Worker secret version metadata is missing or not numeric"
}

Write-Output "LIVE revision verified: approval=$ExpectedMainnetLiveApproved, DISARMED, durable, no order submission"

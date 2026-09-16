<#
.SYNOPSIS
  Cloud-tier release gate orchestrator.

.DESCRIPTION
  Runs the live-cloud verification scripts and the Mainnet read-only preflight
  through the dedicated Control Plane. It never impersonates the Worker or
  calls the Worker directly.

  Exit code: 0 if every check passed, 1 otherwise.
#>
param(
  [string]$ProjectId                  = "gen-lang-client-0730128480",
  [string]$Region                     = "asia-southeast1",
  [string]$ServiceName                = "blessing-trading-worker",
  [string]$RuntimeServiceAccount      = "blessing-runtime@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string]$ControlPlaneServiceAccount = "blessing-control-plane@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string]$ReleaseControllerServiceAccount = "blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string]$ExpectedImageDigest        = "",
  [string]$ControlPlaneImageDigest    = "",
  [string[]]$DatasetIds               = @("market_data", "signals", "risk", "backtests"),
  [string]$ExpectedExecutionMode      = "PAPER",
  [ValidateSet("true", "false")]
  [string]$ExpectedMainnetLiveApproved = "false",
  [Parameter(Mandatory = $true)]
  [string]$ControlPlaneBaseUrl,
  [Parameter(Mandatory = $true)]
  [string]$WorkerBaseUrl,
  [string]$BillingAccount            = "",
  [string]$CandidateId                = ""
)

$ErrorActionPreference = "Stop"

foreach ($serviceUrl in @($ControlPlaneBaseUrl, $WorkerBaseUrl)) {
  if ($serviceUrl -notmatch '^https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])*$' -or
      $serviceUrl -notmatch '\.') {
    throw "Control Plane and Worker URLs must be canonical HTTPS service URLs"
  }
}

# ── Resolve script directory so sub-scripts can be found regardless of CWD ──
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$cloudrunDir = Join-Path (Split-Path -Parent $scriptDir) "cloudrun"
$bigqueryDir = Join-Path (Split-Path -Parent $scriptDir) "bigquery"
$monitoringDir = Join-Path (Split-Path -Parent $scriptDir) "monitoring"

# Keep the token in memory only. It is used for the fixed Control Plane OIDC
# boundary and is intentionally excluded from all evidence written below.
$headers = @{}

# ── Obtain a Control Plane identity token from the attached identity ─────────
# The token is never accepted as a parameter, so it cannot be exposed through
# the process command line or copied into release evidence.
$identityToken = (& gcloud auth print-identity-token --audiences=$ControlPlaneBaseUrl 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($identityToken)) {
  throw "Release Controller OIDC token could not be obtained"
}

# ── Helper: build a check record ─────────────────────────────────────────────
function New-Check {
  param(
    [string]$Id,
    [string]$Name,
    [string]$Status,   # "PASS" or "FAIL"
    [string]$Message
  )
  return [ordered]@{
    id      = $Id
    name    = $Name
    status  = $Status
    message = $Message
  }
}

$checks = [System.Collections.Generic.List[object]]::new()

# ── Check 1: verify-iam.ps1 ───────────────────────────────────────────────────
try {
  & "$cloudrunDir\verify-iam.ps1" `
    -ProjectId $ProjectId `
    -Region $Region `
    -ServiceName $ServiceName `
    -ControlPlaneServiceAccount $ControlPlaneServiceAccount
  $checks.Add((New-Check `
    -Id      "cloud_iam" `
    -Name    "Cloud Run IAM (verify-iam.ps1)" `
    -Status  "PASS" `
    -Message "OK"))
} catch {
  $checks.Add((New-Check `
    -Id      "cloud_iam" `
    -Name    "Cloud Run IAM (verify-iam.ps1)" `
    -Status  "FAIL" `
    -Message $_.Exception.Message))
}

# ── Check 2: release identities and scoped IAM ──────────────────────────────
try {
  & "$cloudrunDir\verify-release-identities.ps1" `
    -ProjectId $ProjectId `
    -Region $Region `
    -WorkerServiceName $ServiceName `
    -ControlPlaneServiceName "blessing-control-plane" `
    -RuntimeServiceAccount $RuntimeServiceAccount `
    -ControlPlaneServiceAccount $ControlPlaneServiceAccount `
    -ReleaseControllerServiceAccount $ReleaseControllerServiceAccount `
    -DatasetIds $DatasetIds
  $checks.Add((New-Check "cloud_release_identities" "Control Plane and Release Controller IAM" "PASS" "OK"))
} catch {
  $checks.Add((New-Check "cloud_release_identities" "Control Plane and Release Controller IAM" "FAIL" $_.Exception.Message))
}

# ── Check 3: verify-disarmed.ps1 ─────────────────────────────────────────────
try {
  if ([string]::IsNullOrWhiteSpace($ExpectedImageDigest)) {
    throw "ExpectedImageDigest is required for immutable Worker read-back"
  }
  & "$cloudrunDir\verify-disarmed.ps1" `
    -ProjectId $ProjectId `
    -Region $Region `
    -ServiceName $ServiceName `
    -RuntimeServiceAccount $RuntimeServiceAccount `
    -ExpectedImageDigest $ExpectedImageDigest `
    -ExpectedExecutionMode $ExpectedExecutionMode `
    -ExpectedMainnetLiveApproved $ExpectedMainnetLiveApproved
  $checks.Add((New-Check `
    -Id      "cloud_disarmed" `
    -Name    "Cloud Run disarmed flags (verify-disarmed.ps1)" `
    -Status  "PASS" `
    -Message "OK"))
} catch {
  $checks.Add((New-Check `
    -Id      "cloud_disarmed" `
    -Name    "Cloud Run disarmed flags (verify-disarmed.ps1)" `
    -Status  "FAIL" `
    -Message $_.Exception.Message))
}

# ── Check 4: verify-runtime-access.ps1 ───────────────────────────────────────
try {
  & "$bigqueryDir\verify-runtime-access.ps1" `
    -ProjectId $ProjectId `
    -RuntimeServiceAccount $RuntimeServiceAccount `
    -DatasetIds $DatasetIds
  $checks.Add((New-Check `
    -Id      "cloud_bq_access" `
    -Name    "BigQuery runtime access (verify-runtime-access.ps1)" `
    -Status  "PASS" `
    -Message "OK"))
} catch {
  $checks.Add((New-Check `
    -Id      "cloud_bq_access" `
    -Name    "BigQuery runtime access (verify-runtime-access.ps1)" `
    -Status  "FAIL" `
    -Message $_.Exception.Message))
}

# ── Check 5: Control Plane deployment read-back ──────────────────────────────
try {
  if ([string]::IsNullOrWhiteSpace($ControlPlaneImageDigest)) {
    throw "ControlPlaneImageDigest is required for Control Plane read-back"
  }
  & "$cloudrunDir\verify-control-plane.ps1" `
    -ProjectId $ProjectId `
    -Region $Region `
    -ServiceName "blessing-control-plane" `
    -ExpectedImageDigest $ControlPlaneImageDigest `
    -ControlPlaneUrl $ControlPlaneBaseUrl `
    -WorkerUrl $WorkerBaseUrl `
    -ReleaseControllerServiceAccount $ReleaseControllerServiceAccount
  $checks.Add((New-Check "cloud_control_plane" "Control Plane deployment" "PASS" "OK"))
} catch {
  $checks.Add((New-Check "cloud_control_plane" "Control Plane deployment" "FAIL" $_.Exception.Message))
}

# ── Check 6: Control Plane protected-route authentication ───────────────────
try {
  & "$cloudrunDir\verify-control-plane-auth.ps1" -ControlPlaneUrl $ControlPlaneBaseUrl
  $checks.Add((New-Check "cloud_control_plane_auth" "Control Plane anonymous/invalid identity denial" "PASS" "OK"))
} catch {
  $checks.Add((New-Check "cloud_control_plane_auth" "Control Plane anonymous/invalid identity denial" "FAIL" $_.Exception.Message))
}

# ── Check 7: monitoring resources read-back ─────────────────────────────────
try {
  & "$monitoringDir\verify-release-monitoring.ps1" -ProjectId $ProjectId
  $checks.Add((New-Check "cloud_monitoring" "Monitoring metrics and alert policies" "PASS" "OK"))
} catch {
  $checks.Add((New-Check "cloud_monitoring" "Monitoring metrics and alert policies" "FAIL" $_.Exception.Message))
}

# ── Check 8: budget alert read-back ──────────────────────────────────────────
try {
  if ([string]::IsNullOrWhiteSpace($BillingAccount)) {
    throw "BillingAccount is required for budget read-back"
  }
  & "$monitoringDir\verify-budget.ps1" -ProjectId $ProjectId -BillingAccount $BillingAccount
  $checks.Add((New-Check "cloud_budget" "Scoped budget alert" "PASS" "OK; alert only"))
} catch {
  $checks.Add((New-Check "cloud_budget" "Scoped budget alert" "FAIL" $_.Exception.Message))
}

# ── Check 9: Control Plane Mainnet read-only preflight ───────────────────────
try {
  $headers = @{ Authorization = "Bearer $identityToken" }
  $response = Invoke-RestMethod `
    -Method Post `
    -Uri "$ControlPlaneBaseUrl/internal/release/preflight" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body "{}" `
    -TimeoutSec 90

  # Determine preflightPassed
  $preflightPassed = $null
  if ($null -ne $response.preflightPassed) {
    $preflightPassed = [bool]$response.preflightPassed
  } elseif ($null -ne $response.preflight_passed) {
    $preflightPassed = [bool]$response.preflight_passed
  }

  # Determine orderSubmissionAttempts (camelCase preferred, snake_case fallback)
  $orderAttempts = $null
  if ($null -ne $response.orderSubmissionAttempts) {
    $orderAttempts = [int]$response.orderSubmissionAttempts
  } elseif ($null -ne $response.order_submission_attempts) {
    $orderAttempts = [int]$response.order_submission_attempts
  }

  $endpointAttempts = $null
  if ($null -ne $response.orderEndpointAttempts) {
    $endpointAttempts = [int]$response.orderEndpointAttempts
  } elseif ($null -ne $response.order_endpoint_attempts) {
    $endpointAttempts = [int]$response.order_endpoint_attempts
  }

  $preflightOk   = ($preflightPassed -eq $true)
  $attemptsOk    = ($orderAttempts -eq 0)

  if ($preflightOk -and $attemptsOk -and $endpointAttempts -eq 0) {
    $checks.Add((New-Check `
      -Id      "cloud_preflight" `
      -Name    "Control Plane Mainnet preflight (read-only, zero order attempts)" `
      -Status  "PASS" `
      -Message "OK"))
  } else {
    $failMsg = "Preflight check failed:"
    if (-not $preflightOk) {
      $failMsg += " preflightPassed=$preflightPassed (expected true);"
    }
    if (-not $attemptsOk) {
      $failMsg += " orderSubmissionAttempts=$orderAttempts (expected 0);"
    }
    if ($endpointAttempts -ne 0) {
      $failMsg += " orderEndpointAttempts=$endpointAttempts (expected 0);"
    }
    $checks.Add((New-Check `
      -Id      "cloud_preflight" `
      -Name    "Control Plane Mainnet preflight (read-only, zero order attempts)" `
      -Status  "FAIL" `
      -Message $failMsg.TrimEnd(';')))
  }
} catch {
  $checks.Add((New-Check `
    -Id      "cloud_preflight" `
    -Name    "Control Plane Mainnet preflight (read-only, zero order attempts)" `
    -Status  "FAIL" `
    -Message "HTTP call failed: $($_.Exception.Message)"))
}

# ── Check 10: candidate verification (when supplied) ────────────────────────
if ($CandidateId) {
  try {
    $verification = Invoke-RestMethod `
      -Method Post `
      -Uri "$ControlPlaneBaseUrl/internal/release/verify" `
      -Headers $headers `
      -ContentType "application/json" `
      -Body (@{ candidateId = $CandidateId } | ConvertTo-Json) `
      -TimeoutSec 90
    if ($verification.verified -eq $true) {
      $checks.Add((New-Check "cloud_release_candidate" "Release candidate verification" "PASS" "OK"))
    } else {
      $checks.Add((New-Check "cloud_release_candidate" "Release candidate verification" "FAIL" "Release prerequisites failed"))
    }
  } catch {
    $checks.Add((New-Check "cloud_release_candidate" "Release candidate verification" "FAIL" "Candidate verification call failed"))
  }
}

# ── Build evidence object ─────────────────────────────────────────────────────
$overallPassed = ($checks | Where-Object { $_.status -ne "PASS" }).Count -eq 0
$generatedAt   = [System.DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")

$evidence = [ordered]@{
  project_id                 = $ProjectId
  region                     = $Region
  control_plane_url          = $ControlPlaneBaseUrl
  worker_url                 = $WorkerBaseUrl
  worker_image_digest        = $ExpectedImageDigest
  control_plane_image_digest = $ControlPlaneImageDigest
  candidate_id               = if ($CandidateId) { $CandidateId } else { $null }
  checks         = @($checks)
  overall_passed = $overallPassed
  generated_at   = $generatedAt
}

# ── Write evidence file ───────────────────────────────────────────────────────
# Repo root is two levels above this script (infra/release_gate/cloud_gate.ps1)
$repoRoot    = Split-Path -Parent (Split-Path -Parent $scriptDir)
$evidenceDir = Join-Path $repoRoot "evidence"
if (-not (Test-Path $evidenceDir)) {
  New-Item -ItemType Directory -Path $evidenceDir | Out-Null
}

$timestamp    = [System.DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$evidenceFile = Join-Path $evidenceDir "cloud-gate-$timestamp.json"
$evidence | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 $evidenceFile

Write-Output "Evidence written to: $evidenceFile"
Write-Output "Overall passed: $overallPassed"

# ── Exit code ─────────────────────────────────────────────────────────────────
if ($overallPassed) {
  exit 0
} else {
  exit 1
}

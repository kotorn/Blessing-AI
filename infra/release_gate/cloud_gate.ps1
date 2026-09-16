<#
.SYNOPSIS
  Cloud-tier release gate orchestrator.

.DESCRIPTION
  Runs all three live-cloud verification scripts (verify-iam.ps1,
  verify-disarmed.ps1, verify-runtime-access.ps1) and the worker's
  /preflight/read-only endpoint, then emits a structured evidence JSON file
  whose schema is identical to the one produced by apps/release_gate/repo_gate.py
  so a combiner script can treat both tiers uniformly.

  Exit code: 0 if every check passed, 1 otherwise.
#>
param(
  [string]$ProjectId                  = "gen-lang-client-0730128480",
  [string]$Region                     = "asia-southeast1",
  [string]$ServiceName                = "blessing-trading-worker",
  [string]$RuntimeServiceAccount      = "blessing-runtime@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string]$ControlPlaneServiceAccount = "blessing-control-plane@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string]$ExpectedImageDigest        = "",
  [string[]]$DatasetIds               = @("market_data", "signals", "risk", "backtests"),
  [string]$ExpectedExecutionMode      = "PAPER",
  [Parameter(Mandatory = $true)]
  [string]$WorkerBaseUrl,
  [string]$IdentityToken              = ""
)

$ErrorActionPreference = "Stop"

# ── Resolve script directory so sub-scripts can be found regardless of CWD ──
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$cloudrunDir = Join-Path (Split-Path -Parent $scriptDir) "cloudrun"
$bigqueryDir = Join-Path (Split-Path -Parent $scriptDir) "bigquery"

# ── Obtain an identity token if not supplied ─────────────────────────────────
if ([string]::IsNullOrWhiteSpace($IdentityToken)) {
  Write-Output "Obtaining identity token via gcloud impersonation..."
  $IdentityToken = & gcloud auth print-identity-token `
    --impersonate-service-account=$ControlPlaneServiceAccount `
    --audiences=$WorkerBaseUrl
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to obtain identity token via gcloud"
  }
  $IdentityToken = $IdentityToken.Trim()
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

# ── Check 2: verify-disarmed.ps1 ─────────────────────────────────────────────
try {
  & "$cloudrunDir\verify-disarmed.ps1" `
    -ProjectId $ProjectId `
    -Region $Region `
    -ServiceName $ServiceName `
    -RuntimeServiceAccount $RuntimeServiceAccount `
    -ExpectedImageDigest $ExpectedImageDigest `
    -ExpectedExecutionMode $ExpectedExecutionMode
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

# ── Check 3: verify-runtime-access.ps1 ───────────────────────────────────────
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

# ── Check 4: POST /preflight/read-only ───────────────────────────────────────
try {
  $headers = @{ Authorization = "Bearer $IdentityToken" }
  $response = Invoke-RestMethod `
    -Method Post `
    -Uri "$WorkerBaseUrl/preflight/read-only" `
    -Headers $headers `
    -TimeoutSec 60

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

  $preflightOk   = ($preflightPassed -eq $true)
  $attemptsOk    = ($orderAttempts -eq 0)

  if ($preflightOk -and $attemptsOk) {
    $checks.Add((New-Check `
      -Id      "cloud_preflight" `
      -Name    "Worker /preflight/read-only (preflightPassed=true, orderSubmissionAttempts=0)" `
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
    $checks.Add((New-Check `
      -Id      "cloud_preflight" `
      -Name    "Worker /preflight/read-only (preflightPassed=true, orderSubmissionAttempts=0)" `
      -Status  "FAIL" `
      -Message $failMsg.TrimEnd(';')))
  }
} catch {
  $checks.Add((New-Check `
    -Id      "cloud_preflight" `
    -Name    "Worker /preflight/read-only (preflightPassed=true, orderSubmissionAttempts=0)" `
    -Status  "FAIL" `
    -Message "HTTP call failed: $($_.Exception.Message)"))
}

# ── Build evidence object ─────────────────────────────────────────────────────
$overallPassed = ($checks | Where-Object { $_.status -ne "PASS" }).Count -eq 0
$generatedAt   = [System.DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")

$evidence = [ordered]@{
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

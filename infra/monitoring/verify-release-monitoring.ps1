<#
.SYNOPSIS
  Read back the required Blessing AI log metrics and alert policies.

This script is read-only. It does not create monitoring resources and does not
interpret monitoring as a spending cap or as trading authorization.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string[]]$MetricNames = @(),
  [string[]]$PolicyNames = @()
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($MetricNames.Count -eq 0) {
  $MetricNames = @(
    (Get-Content (Join-Path $scriptDir "log-metrics.json") -Raw | ConvertFrom-Json) |
      ForEach-Object { [string]$_.name }
  )
}
if ($PolicyNames.Count -eq 0) {
  $PolicyNames = @(
    (Get-Content (Join-Path $scriptDir "alert-policies.json") -Raw | ConvertFrom-Json) |
      ForEach-Object { [string]$_.displayName }
  )
}

# Use value(name) / value(displayName) instead of --format=json: the PS 5.1
# pipeline + this gcloud build merges multi-object JSON into a single object,
# making every name look absent. Single-line values parse deterministically.
$actualMetricNames = @(& gcloud logging metrics list --project=$ProjectId --format="value(name)")
if ($LASTEXITCODE -ne 0) { throw "Unable to read Cloud Logging log-based metrics" }
$missingMetrics = @($MetricNames | Where-Object { $_ -notin $actualMetricNames })
if ($missingMetrics.Count -gt 0) {
  throw "Required log-based metrics are missing: $($missingMetrics -join ', ')"
}

$actualPolicyNames = @(& gcloud monitoring policies list --project=$ProjectId --format="value(displayName)")
if ($LASTEXITCODE -ne 0) { throw "Unable to read Cloud Monitoring alert policies" }
$missingPolicies = @($PolicyNames | Where-Object { $_ -notin $actualPolicyNames })
if ($missingPolicies.Count -gt 0) {
  throw "Required alert policies are missing: $($missingPolicies -join ', ')"
}

Write-Output "Monitoring read-back verified: $($MetricNames.Count) log metrics and $($PolicyNames.Count) alert policies"
Write-Output "Monitoring is evidence/alerting only; it is not a spending cap or Mainnet authorization"

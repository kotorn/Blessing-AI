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

$metricJson = & gcloud logging metrics list --project=$ProjectId --format=json
if ($LASTEXITCODE -ne 0) { throw "Unable to read Cloud Logging log-based metrics" }
$metrics = @($metricJson | ConvertFrom-Json)
$actualMetricNames = @($metrics | ForEach-Object { [string]$_.name })
$missingMetrics = @($MetricNames | Where-Object { $_ -notin $actualMetricNames })
if ($missingMetrics.Count -gt 0) {
  throw "Required log-based metrics are missing: $($missingMetrics -join ', ')"
}

$policyJson = & gcloud monitoring policies list --project=$ProjectId --format=json
if ($LASTEXITCODE -ne 0) { throw "Unable to read Cloud Monitoring alert policies" }
$policies = @($policyJson | ConvertFrom-Json)
$actualPolicyNames = @($policies | ForEach-Object {
  if ($null -ne $_.displayName) { [string]$_.displayName } else { [string]$_.display_name }
})
$missingPolicies = @($PolicyNames | Where-Object { $_ -notin $actualPolicyNames })
if ($missingPolicies.Count -gt 0) {
  throw "Required alert policies are missing: $($missingPolicies -join ', ')"
}

Write-Output "Monitoring read-back verified: $($MetricNames.Count) log metrics and $($PolicyNames.Count) alert policies"
Write-Output "Monitoring is evidence/alerting only; it is not a spending cap or Mainnet authorization"

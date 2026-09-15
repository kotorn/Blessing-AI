param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$BillingAccount = ""
)

$ErrorActionPreference = "Stop"
$monitoringRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Invoke-GCloud {
  param([Parameter(Mandatory = $true)][string[]]$Arguments)

  & gcloud @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "gcloud command failed with exit code ${LASTEXITCODE}: gcloud $($Arguments -join ' ')"
  }
}

Invoke-GCloud @(
  "services", "enable",
  "logging.googleapis.com",
  "monitoring.googleapis.com",
  "billingbudgets.googleapis.com",
  "--project=$ProjectId",
  "--quiet"
)

$metricDefinitions = Get-Content (Join-Path $monitoringRoot "log-metrics.json") -Raw | ConvertFrom-Json
$existingMetricNames = @(
  & gcloud logging metrics list --project=$ProjectId --format="value(name)"
)
if ($LASTEXITCODE -ne 0) {
  throw "Unable to read existing log-based metrics"
}

foreach ($metric in $metricDefinitions) {
  $metricName = [string]$metric.name
  $metricArgs = @(
    $metricName,
    "--description=$($metric.description)",
    "--log-filter=$($metric.filter)",
    "--project=$ProjectId",
    "--quiet"
  )
  if ($existingMetricNames -contains $metricName) {
    Invoke-GCloud (@("logging", "metrics", "update") + $metricArgs)
  } else {
    Invoke-GCloud (@("logging", "metrics", "create") + $metricArgs)
  }
}

$tempPolicyRoot = Join-Path ([System.IO.Path]::GetTempPath()) "blessing-monitoring-$PID"
New-Item -ItemType Directory -Path $tempPolicyRoot -Force | Out-Null
try {
  $policyDefinitions = Get-Content (Join-Path $monitoringRoot "alert-policies.json") -Raw | ConvertFrom-Json
  $existingPoliciesJson = & gcloud monitoring policies list --project=$ProjectId --format=json
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to read existing monitoring alert policies"
  }
  $existingPolicies = @($existingPoliciesJson | ConvertFrom-Json)

  $policyIndex = 0
  foreach ($policy in $policyDefinitions) {
    $policyIndex++
    $displayName = [string]$policy.displayName
    $existing = @($existingPolicies | Where-Object { $_.displayName -eq $displayName }) | Select-Object -First 1
    if ($null -ne $existing) {
      # Do not overwrite operator-managed notification channels. The policy
      # is already present and its live read-back remains the acceptance gate.
      Write-Output "Monitoring policy already exists; leaving operator channels unchanged: $displayName"
      continue
    }

    $policyPath = Join-Path $tempPolicyRoot ("policy-$policyIndex.json")
    $policy | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $policyPath -Encoding utf8
    Invoke-GCloud @(
      "monitoring", "policies", "create",
      "--policy-from-file=$policyPath",
      "--project=$ProjectId",
      "--quiet"
    )
  }
}
finally {
  if (Test-Path -LiteralPath $tempPolicyRoot) {
    Remove-Item -LiteralPath $tempPolicyRoot -Recurse -Force
  }
}

if ([string]::IsNullOrWhiteSpace($BillingAccount)) {
  Write-Output "Monitoring metrics and policies applied. Budget creation skipped: pass -BillingAccount after read-back."
  exit 0
}

$budgetName = "Blessing AI monthly alert budget"
$budgetsJson = & gcloud beta billing budgets list --billing-account=$BillingAccount --format=json
if ($LASTEXITCODE -ne 0) {
  throw "Unable to read existing billing budgets"
}
$budgets = @($budgetsJson | ConvertFrom-Json)
$existingBudget = @($budgets | Where-Object { $_.displayName -eq $budgetName }) | Select-Object -First 1
if ($null -eq $existingBudget) {
  Invoke-GCloud @(
    "beta", "billing", "budgets", "create",
    "--billing-account=$BillingAccount",
    "--display-name=$budgetName",
    "--budget-amount=10",
    "--calendar-period=month",
    "--filter-projects=projects/$ProjectId",
    "--threshold-rule=basis=current-spend,percent=0.50",
    "--threshold-rule=basis=current-spend,percent=0.75",
    "--threshold-rule=basis=current-spend,percent=0.90",
    "--threshold-rule=basis=forecasted-spend,percent=1.00",
    "--quiet"
  )
} else {
  # Reconcile the declarative scope and thresholds on every apply. This keeps
  # an accidentally unscoped budget from silently covering the wrong projects
  # while preserving operator-managed notification settings.
  Invoke-GCloud @(
    "beta", "billing", "budgets", "update",
    ([string]$existingBudget.name),
    "--billing-account=$BillingAccount",
    "--budget-amount=10",
    "--calendar-period=month",
    "--filter-projects=projects/$ProjectId",
    "--clear-threshold-rules",
    "--add-threshold-rule=basis=current-spend,percent=0.50",
    "--add-threshold-rule=basis=current-spend,percent=0.75",
    "--add-threshold-rule=basis=current-spend,percent=0.90",
    "--add-threshold-rule=basis=forecasted-spend,percent=1.00",
    "--quiet"
  )
  Write-Output "Budget scope and thresholds reconciled: $budgetName"
}

Write-Output "Monitoring and budget alert resources applied for $ProjectId. Alerts do not cap spending or authorize Mainnet."

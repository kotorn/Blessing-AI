<#
.SYNOPSIS
  Read back the scoped Blessing AI monthly budget alert.

Google Cloud Billing Budgets notify operators; they do not stop spending and
cannot authorize or arm the trading Worker.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$BillingAccount = "",
  [string]$ExpectedDisplayName = "Blessing AI monthly alert budget"
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($BillingAccount)) {
  throw "BillingAccount is required for budget read-back"
}

$budgetJson = & gcloud beta billing budgets list --billing-account=$BillingAccount --format=json
if ($LASTEXITCODE -ne 0) { throw "Unable to read billing budgets" }
$budgets = @($budgetJson | ConvertFrom-Json)
$budget = @($budgets | Where-Object { [string]$_.displayName -eq $ExpectedDisplayName }) | Select-Object -First 1
if ($null -eq $budget) { throw "Required scoped budget alert is missing: $ExpectedDisplayName" }

$projects = @()
if ($null -ne $budget.budgetFilter -and $null -ne $budget.budgetFilter.projects) {
  $projects = @($budget.budgetFilter.projects | ForEach-Object { [string]$_ })
} elseif ($null -ne $budget.filter -and $null -ne $budget.filter.projects) {
  $projects = @($budget.filter.projects | ForEach-Object { [string]$_ })
}
$projectNumber = (& gcloud projects describe $ProjectId --format="value(projectNumber)" 2>$null).Trim()
$expectedProjects = @("projects/$ProjectId")
if (-not [string]::IsNullOrWhiteSpace($projectNumber)) {
  $expectedProjects += "projects/$projectNumber"
}
if ($projects.Count -ne 1 -or $projects[0] -notin $expectedProjects) {
  throw "Budget must be scoped only to projects/$ProjectId"
}

$thresholdRules = @($budget.thresholdRules)
if ($thresholdRules.Count -eq 0) { throw "Budget has no threshold alert rules" }

Write-Output "Budget read-back verified: $ExpectedDisplayName scoped only to $expectedProject"
Write-Output "Budget is an alert only; it is not a hard spending cap or trading authorization"

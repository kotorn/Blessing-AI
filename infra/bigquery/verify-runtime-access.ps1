param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$RuntimeServiceAccount = "blessing-runtime@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string[]]$DatasetIds = @("market_data", "signals", "risk", "backtests")
)

$ErrorActionPreference = "Stop"
$allowedDatasets = @("market_data", "signals", "risk", "backtests")
if ($DatasetIds | Where-Object { $_ -notin $allowedDatasets }) {
  throw "DatasetIds must be limited to market_data, signals, risk, and backtests"
}
if ($RuntimeServiceAccount -notmatch '^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$') {
  throw "RuntimeServiceAccount must be a service-account email"
}

$projectPolicyJson = & gcloud projects get-iam-policy $ProjectId --format=json
if ($LASTEXITCODE -ne 0) {
  throw "Unable to read project IAM policy"
}
$projectPolicy = $projectPolicyJson | ConvertFrom-Json
foreach ($binding in @($projectPolicy.bindings)) {
  if ([string]$binding.role -ne "roles/bigquery.dataEditor") {
    continue
  }
  if (@($binding.members) -contains "serviceAccount:$RuntimeServiceAccount") {
    throw "Runtime service account still has project-wide roles/bigquery.dataEditor"
  }
}

foreach ($datasetId in $DatasetIds) {
  $datasetRef = "${ProjectId}:${datasetId}"
  $datasetJson = & bq show --format=json --dataset_view=FULL $datasetRef
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to read BigQuery dataset $datasetRef"
  }
  $dataset = $datasetJson | ConvertFrom-Json
  $hasWriter = @(
    @($dataset.access) | Where-Object {
      $_.role -eq "WRITER" -and $_.userByEmail -eq $RuntimeServiceAccount
    }
  ).Count -gt 0
  if (-not $hasWriter) {
    throw "Dataset-scoped WRITER access is missing for $datasetRef"
  }
  Write-Output "BigQuery dataset access verified: $datasetRef"
}

Write-Output "No project-wide BigQuery dataEditor binding and all dataset grants verified."

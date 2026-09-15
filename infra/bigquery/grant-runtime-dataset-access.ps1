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

function Invoke-Bq {
  param([Parameter(Mandatory = $true)][string[]]$Arguments)

  & bq @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "bq command failed with exit code ${LASTEXITCODE}: bq $($Arguments -join ' ')"
  }
}

foreach ($datasetId in $DatasetIds) {
  $datasetRef = "${ProjectId}:${datasetId}"
  $datasetJson = & bq show --format=json --dataset_view=FULL $datasetRef
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to read BigQuery dataset $datasetRef"
  }
  $dataset = $datasetJson | ConvertFrom-Json
  $access = @($dataset.access)
  $hasRuntimeWriter = @(
    $access | Where-Object {
      $_.role -eq "WRITER" -and $_.userByEmail -eq $RuntimeServiceAccount
    }
  ).Count -gt 0
  if ($hasRuntimeWriter) {
    Write-Output "Dataset access already present: $datasetRef"
    continue
  }

  $access += [ordered]@{
    role = "WRITER"
    userByEmail = $RuntimeServiceAccount
  }
  $update = [ordered]@{
    datasetReference = [ordered]@{
      projectId = $ProjectId
      datasetId = $datasetId
    }
    access = $access
  }
  $tempFile = Join-Path ([System.IO.Path]::GetTempPath()) "blessing-bq-$([Guid]::NewGuid().ToString('N')).json"
  try {
    $update | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $tempFile -Encoding utf8
    Invoke-Bq @("update", "--dataset", "--source=$tempFile", $datasetRef)
  }
  finally {
    if (Test-Path -LiteralPath $tempFile) {
      Remove-Item -LiteralPath $tempFile -Force
    }
  }
  Write-Output "Granted dataset-scoped BigQuery WRITER access: $datasetRef"
}

Write-Output "Dataset-scoped access completed for $($DatasetIds -join ', ')."

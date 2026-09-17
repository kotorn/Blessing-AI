<#
.SYNOPSIS
  Read back the fixed Control Plane and Release Controller IAM boundary.

.DESCRIPTION
  This is a read-only acceptance check. It verifies service-account existence,
  the Worker invoker allowlist, absence of public Worker invocation, scoped
  BigQuery access, and the absence of user-managed service-account keys or
  Secret Manager accessor grants for the controller identities.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Region = "asia-southeast1",
  [string]$WorkerServiceName = "blessing-trading-worker",
  [string]$ControlPlaneServiceName = "blessing-control-plane",
  [string]$RuntimeServiceAccount = "blessing-runtime@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string]$ControlPlaneServiceAccount = "blessing-control-plane@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string]$ReleaseControllerServiceAccount = "blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string[]]$DatasetIds = @("market_data", "signals", "risk", "backtests")
)

$ErrorActionPreference = "Stop"
$expectedEmails = @($RuntimeServiceAccount, $ControlPlaneServiceAccount, $ReleaseControllerServiceAccount)
foreach ($email in $expectedEmails) {
  if ($email -notmatch '^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$') {
    throw "Invalid service-account email"
  }
  & gcloud iam service-accounts describe $email --project=$ProjectId --format="value(email)" 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Required service account is missing: $email" }
}

function Read-GCloudJson {
  param([Parameter(Mandatory = $true)][string[]]$Arguments)
  $raw = & gcloud @Arguments
  if ($LASTEXITCODE -ne 0) { throw "Unable to read required Google Cloud state" }
  try { return ($raw | ConvertFrom-Json) } catch { throw "Google Cloud read-back was not valid JSON" }
}

function Read-BqJson {
  param([Parameter(Mandatory = $true)][string[]]$Arguments)
  $raw = & bq @Arguments
  if ($LASTEXITCODE -ne 0) { throw "Unable to read required BigQuery state" }
  try { return ($raw | ConvertFrom-Json) } catch { throw "BigQuery read-back was not valid JSON" }
}

$projectPolicy = Read-GCloudJson @("projects", "get-iam-policy", $ProjectId, "--format=json")
foreach ($binding in @($projectPolicy.bindings)) {
  $role = [string]$binding.role
  $members = @($binding.members) | ForEach-Object { [string]$_ }
  if ($role -eq "roles/secretmanager.secretAccessor" -and ($members | Where-Object {
        $_ -in @("serviceAccount:$ControlPlaneServiceAccount", "serviceAccount:$ReleaseControllerServiceAccount")
      })) {
    throw "Control Plane or Release Controller has a project-wide Secret Manager accessor grant"
  }
  if ($role -eq "roles/bigquery.dataEditor" -and ($members | Where-Object {
        $_ -in @("serviceAccount:$RuntimeServiceAccount", "serviceAccount:$ControlPlaneServiceAccount", "serviceAccount:$ReleaseControllerServiceAccount")
      })) {
    throw "A release identity still has project-wide BigQuery dataEditor"
  }
}

function Read-ServiceIam {
  param([Parameter(Mandatory = $true)][string]$ServiceName)
  return Read-GCloudJson @(
    "run", "services", "get-iam-policy", $ServiceName,
    "--project=$ProjectId", "--region=$Region", "--format=json"
  )
}

$workerPolicy = Read-ServiceIam $WorkerServiceName
$workerInvokerMembers = @(
  foreach ($binding in @($workerPolicy.bindings)) {
    if ([string]$binding.role -eq "roles/run.invoker") {
      @($binding.members) | ForEach-Object { [string]$_ }
    }
  }
)
$expectedWorkerMember = "serviceAccount:$ControlPlaneServiceAccount"
if ($workerInvokerMembers -contains "allUsers" -or $workerInvokerMembers -contains "allAuthenticatedUsers") {
  throw "Worker still allows public or all-authenticated invocation"
}
if ($workerInvokerMembers.Count -ne 1 -or $workerInvokerMembers[0] -ne $expectedWorkerMember) {
  throw "Worker invoker policy is not exactly the Control Plane service account"
}
if ($workerInvokerMembers -contains "serviceAccount:$ReleaseControllerServiceAccount") {
  throw "Release Controller must not invoke the Worker directly"
}

$controlKeys = @(& gcloud iam service-accounts keys list "--iam-account=$ControlPlaneServiceAccount" --project=$ProjectId --managed-by=user --format="value(name)" 2>$null)
if ($LASTEXITCODE -ne 0 -or $controlKeys.Count -gt 0) { throw "Control Plane must not use user-managed service-account keys" }
$releaseKeys = @(& gcloud iam service-accounts keys list "--iam-account=$ReleaseControllerServiceAccount" --project=$ProjectId --managed-by=user --format="value(name)" 2>$null)
if ($LASTEXITCODE -ne 0 -or $releaseKeys.Count -gt 0) { throw "Release Controller must not use user-managed service-account keys" }

foreach ($datasetId in $DatasetIds) {
  if ($datasetId -notin @("market_data", "signals", "risk", "backtests")) {
    throw "DatasetIds are outside the Blessing AI allowlist"
  }
  $dataset = Read-BqJson @("show", "--format=json", "--dataset_view=FULL", "${ProjectId}:$datasetId")
  $access = @($dataset.access)
  $reader = @($access | Where-Object {
    ([string]$_.userByEmail -eq $ControlPlaneServiceAccount) -and ([string]$_.role -eq "READER")
  })
  if ($reader.Count -eq 0) { throw "Control Plane dataset reader grant is missing: $datasetId" }
  $writerOrOwner = @($access | Where-Object {
    ([string]$_.userByEmail -eq $ControlPlaneServiceAccount) -and ([string]$_.role -in @("WRITER", "OWNER"))
  })
  if ($writerOrOwner.Count -gt 0) { throw "Control Plane has excessive BigQuery access on $datasetId" }
}

& gcloud run services describe $ControlPlaneServiceName --project=$ProjectId --region=$Region --format="value(metadata.name)" 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
  Write-Output "Control Plane service exists: $ControlPlaneServiceName"
} else {
  Write-Output "Control Plane service is not deployed yet; identity verification remains fail-closed for deployment"
}

Write-Output "Release identities verified: Worker is private, controllers have no secret access or keys, BigQuery is dataset-scoped"

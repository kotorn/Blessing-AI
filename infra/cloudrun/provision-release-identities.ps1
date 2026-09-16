<#[
.SYNOPSIS
  Provision the fixed Control Plane and Release Controller identities.

.DESCRIPTION
  This is an explicit administrator operation. Without -Apply it only shows
  the intended scope. It never creates keys, reads secret values, impersonates
  a service account, or grants the Release Controller access to Binance/SQL
  secrets. The Worker invoker binding is reconciled to the Control Plane
  service account only.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Region = "asia-southeast1",
  [string]$ArtifactRepository = "blessing-repo",
  [string]$WorkerServiceName = "blessing-trading-worker",
  [string]$ControlPlaneServiceName = "blessing-control-plane",
  [string]$ControlPlaneServiceAccount = "blessing-control-plane@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [string]$ReleaseControllerServiceAccount = "blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com",
  [switch]$Apply
)

$ErrorActionPreference = "Stop"

if ($ControlPlaneServiceAccount -notmatch '^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$') {
  throw "ControlPlaneServiceAccount must be a service-account email"
}
if ($ReleaseControllerServiceAccount -notmatch '^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$') {
  throw "ReleaseControllerServiceAccount must be a service-account email"
}

function Invoke-GCloud {
  param([Parameter(Mandatory = $true)][string[]]$Arguments)
  & gcloud @Arguments | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "gcloud command failed with exit code ${LASTEXITCODE}"
  }
}

function Test-ServiceAccount {
  param([Parameter(Mandatory = $true)][string]$Email)
  & gcloud iam service-accounts describe $Email --project=$ProjectId --format="value(email)" 2>$null | Out-Null
  return $LASTEXITCODE -eq 0
}

function Ensure-ServiceAccount {
  param(
    [Parameter(Mandatory = $true)][string]$AccountId,
    [Parameter(Mandatory = $true)][string]$Email,
    [Parameter(Mandatory = $true)][string]$DisplayName
  )
  if (Test-ServiceAccount $Email) {
    Write-Output "Service account exists: $Email"
    return
  }
  if (-not $Apply) {
    Write-Output "DRY_RUN would create service account: $Email"
    return
  }
  Invoke-GCloud @(
    "iam", "service-accounts", "create", $AccountId,
    "--project=$ProjectId",
    "--display-name=$DisplayName",
    "--description=Blessing AI fixed release boundary identity",
    "--quiet"
  )
  Write-Output "Created service account: $Email"
}

function Grant-ProjectRole {
  param(
    [Parameter(Mandatory = $true)][string]$Member,
    [Parameter(Mandatory = $true)][string]$Role
  )
  if (-not $Apply) {
    Write-Output "DRY_RUN would grant $Role to $Member"
    return
  }
  Invoke-GCloud @(
    "projects", "add-iam-policy-binding", $ProjectId,
    "--member=$Member",
    "--role=$Role",
    "--quiet"
  )
}

function Grant-ServiceAccountRole {
  param(
    [Parameter(Mandatory = $true)][string]$TargetEmail,
    [Parameter(Mandatory = $true)][string]$Member,
    [Parameter(Mandatory = $true)][string]$Role
  )
  if (-not $Apply) {
    Write-Output "DRY_RUN would grant $Role on $TargetEmail to $Member"
    return
  }
  Invoke-GCloud @(
    "iam", "service-accounts", "add-iam-policy-binding", $TargetEmail,
    "--project=$ProjectId",
    "--member=$Member",
    "--role=$Role",
    "--quiet"
  )
}

function Grant-RepositoryRole {
  param(
    [Parameter(Mandatory = $true)][string]$Member,
    [Parameter(Mandatory = $true)][string]$Role
  )
  if (-not $Apply) {
    Write-Output "DRY_RUN would grant repository-scoped $Role to $Member"
    return
  }
  Invoke-GCloud @(
    "artifacts", "repositories", "add-iam-policy-binding", $ArtifactRepository,
    "--location=$Region",
    "--project=$ProjectId",
    "--member=$Member",
    "--role=$Role",
    "--quiet"
  )
}

function Grant-DatasetReader {
  param(
    [Parameter(Mandatory = $true)][string]$Member,
    [Parameter(Mandatory = $true)][string]$DatasetId
  )
  if (-not $Apply) {
    Write-Output "DRY_RUN would grant dataset-scoped BigQuery reader on $DatasetId to $Member"
    return
  }
  & bq add-iam-policy-binding "${ProjectId}:${DatasetId}" `
    --member=$Member `
    --role=roles/bigquery.dataViewer `
    --quiet | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to grant dataset-scoped BigQuery reader on $DatasetId"
  }
}

function Get-WorkerInvokerMembers {
  $policyJson = & gcloud run services get-iam-policy $WorkerServiceName `
    --project=$ProjectId `
    --region=$Region `
    --format=json 2>$null
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to read the Worker invoker policy before changing it"
  }
  try {
    $policy = $policyJson | ConvertFrom-Json
  } catch {
    throw "Worker invoker policy read-back was not valid JSON"
  }
  return @(
    foreach ($binding in @($policy.bindings)) {
      if ([string]$binding.role -eq "roles/run.invoker") {
        @($binding.members) | ForEach-Object { [string]$_ }
      }
    }
  )
}

if (-not $Apply) {
  Write-Output "DRY_RUN: no IAM or service-account changes will be made. Re-run with -Apply after review."
}

$controlMember = "serviceAccount:$ControlPlaneServiceAccount"
$releaseMember = "serviceAccount:$ReleaseControllerServiceAccount"
Ensure-ServiceAccount "blessing-control-plane" $ControlPlaneServiceAccount "Blessing AI Control Plane"
Ensure-ServiceAccount "blessing-release-controller" $ReleaseControllerServiceAccount "Blessing AI Release Controller"

# Control Plane needs only server-side Firestore access and BigQuery jobs/readers.
Grant-ProjectRole $controlMember "roles/datastore.user"
Grant-ProjectRole $controlMember "roles/bigquery.jobUser"
foreach ($dataset in @("market_data", "signals", "risk", "backtests")) {
  Grant-DatasetReader $controlMember $dataset
}

# Release Controller can read artifacts and deploy through the reviewed custom
# role. It receives no Secret Manager role and no Worker invoker role.
Grant-RepositoryRole $releaseMember "roles/artifactregistry.reader"
Grant-ServiceAccountRole "blessing-runtime@${ProjectId}.iam.gserviceaccount.com" $releaseMember "roles/iam.serviceAccountUser"
Grant-ServiceAccountRole $ControlPlaneServiceAccount $releaseMember "roles/iam.serviceAccountUser"

if ($Apply) {
  $roleName = "projects/$ProjectId/roles/blessingReleaseDeployer"
  $roleJson = & gcloud iam roles describe blessingReleaseDeployer --project=$ProjectId --format=json 2>$null
  if ($LASTEXITCODE -ne 0) {
    $tempRole = Join-Path ([System.IO.Path]::GetTempPath()) "blessing-release-role-$([Guid]::NewGuid().ToString('N')).json"
    try {
      $roleDefinition = [ordered]@{
        title = "Blessing AI Release Deployer"
        description = "Deploy only reviewed Blessing AI Cloud Run release revisions"
        stage = "GA"
        includedPermissions = @(
          "resourcemanager.projects.get",
          "run.locations.get",
           "run.services.create",
           "run.services.get",
           "run.services.getIamPolicy",
           "run.services.setIamPolicy",
           "run.services.update",
           "run.revisions.get",
           "run.operations.get"
        )
      }
      $roleDefinition | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $tempRole -Encoding utf8
      Invoke-GCloud @(
        "iam", "roles", "create", "blessingReleaseDeployer",
        "--project=$ProjectId",
        "--file=$tempRole",
        "--quiet"
      )
    }
    finally {
      if (Test-Path -LiteralPath $tempRole) { Remove-Item -LiteralPath $tempRole -Force }
    }
  }
  Grant-ProjectRole $releaseMember $roleName
} else {
  Write-Output "DRY_RUN would grant projects/$ProjectId/roles/blessingReleaseDeployer to $releaseMember"
}

# Remove the dangerous/public Worker bindings if present, then add exactly the
# Control Plane invoker. This is the only service-to-service Worker boundary.
if ($Apply) {
  $membersBefore = @(Get-WorkerInvokerMembers)
  foreach ($member in @("allUsers", "allAuthenticatedUsers")) {
    if ($membersBefore -contains $member) {
      Invoke-GCloud @(
        "run", "services", "remove-iam-policy-binding", $WorkerServiceName,
        "--project=$ProjectId",
        "--region=$Region",
        "--member=$member",
        "--role=roles/run.invoker",
        "--quiet"
      )
    }
  }
  $membersAfterPublicRemoval = @(Get-WorkerInvokerMembers)
  if ($membersAfterPublicRemoval -contains "allUsers" -or $membersAfterPublicRemoval -contains "allAuthenticatedUsers") {
    throw "Worker public invoker binding could not be removed"
  }
  if ($membersAfterPublicRemoval -notcontains $controlMember) {
    Invoke-GCloud @(
      "run", "services", "add-iam-policy-binding", $WorkerServiceName,
      "--project=$ProjectId",
      "--region=$Region",
      "--member=$controlMember",
      "--role=roles/run.invoker",
      "--quiet"
    )
  }
  $finalWorkerMembers = @(Get-WorkerInvokerMembers)
  if ($finalWorkerMembers.Count -ne 1 -or $finalWorkerMembers[0] -ne $controlMember) {
    throw "Worker invoker policy is not exactly the Control Plane service account after reconciliation"
  }
  if (& gcloud run services describe $ControlPlaneServiceName --project=$ProjectId --region=$Region --format="value(metadata.name)" 2>$null) {
    Invoke-GCloud @(
      "run", "services", "add-iam-policy-binding", $ControlPlaneServiceName,
      "--project=$ProjectId",
      "--region=$Region",
      "--member=$releaseMember",
      "--role=roles/run.invoker",
      "--quiet"
    )
  }
} else {
  Write-Output "DRY_RUN would remove allUsers/allAuthenticatedUsers from $WorkerServiceName and grant only $controlMember roles/run.invoker"
}

Write-Output "Identity provisioning scope prepared. Release Controller has no Binance/SQL Secret Manager access and no direct Worker invoker role."

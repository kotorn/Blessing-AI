param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Region = "asia-southeast1",
  [string]$ServiceName = "blessing-trading-worker",
  [string]$ControlPlaneServiceAccount = "blessing-control-plane@gen-lang-client-0730128480.iam.gserviceaccount.com"
)

$ErrorActionPreference = "Stop"

if ($ControlPlaneServiceAccount -notmatch '^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$') {
  throw "ControlPlaneServiceAccount must be a service-account email"
}

$policyJson = & gcloud run services get-iam-policy $ServiceName `
  --project=$ProjectId `
  --region=$Region `
  --format=json
if ($LASTEXITCODE -ne 0) {
  throw "Unable to read Cloud Run IAM policy"
}

try {
  $policy = $policyJson | ConvertFrom-Json
} catch {
  throw "Cloud Run IAM policy is not valid JSON"
}

$invokerMembers = @()
foreach ($binding in @($policy.bindings)) {
  if ([string]$binding.role -ne "roles/run.invoker") {
    continue
  }
  foreach ($member in @($binding.members)) {
    $invokerMembers += [string]$member
  }
}

$expectedMember = "serviceAccount:$ControlPlaneServiceAccount"
if ($invokerMembers -contains "allUsers" -or $invokerMembers -contains "allAuthenticatedUsers") {
  throw "Anonymous Cloud Run invocation is still enabled"
}
if ($invokerMembers.Count -ne 1 -or $invokerMembers[0] -ne $expectedMember) {
  throw "Cloud Run invoker policy must contain only $expectedMember"
}

Write-Output "Cloud Run IAM verified: only $expectedMember can invoke $ServiceName"

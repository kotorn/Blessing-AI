<#
.SYNOPSIS
  Create a sanitized, one-time Mainnet release candidate through the Control Plane.

.DESCRIPTION
  This is a release-metadata operation only. It uses the attached dedicated
  Release Controller identity, sends no Binance/SQL credential values, and
  never arms the Worker or submits an exchange order. Run it only after the
  repository and cloud evidence hashes have been independently produced.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [Parameter(Mandatory = $true)]
  [string]$ControlPlaneUrl,
  [Parameter(Mandatory = $true)]
  [string]$RepoSha,
  [Parameter(Mandatory = $true)]
  [string]$ImageUri,
  [Parameter(Mandatory = $true)]
  [string]$WorkerRevision,
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[1-9][0-9]*$')]
  [string]$CloudSqlPasswordVersion,
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[1-9][0-9]*$')]
  [string]$BinanceMainnetApiKeyVersion,
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[1-9][0-9]*$')]
  [string]$BinanceMainnetApiSecretVersion,
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[0-9a-fA-F]{64}$')]
  [string]$PreflightEvidenceHash,
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[0-9a-fA-F]{64}$')]
  [string]$RepoGateEvidenceHash,
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[0-9a-fA-F]{64}$')]
  [string]$CloudGateEvidenceHash,
  [Parameter(Mandatory = $true)]
  [string]$ExpiresAt,
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[A-Za-z0-9_-]{16,128}$')]
  [string]$Nonce,
  [string]$ReleaseControllerServiceAccount = "blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com"
)

$ErrorActionPreference = "Stop"

if ($ProjectId -ne "gen-lang-client-0730128480") {
  throw "Release candidate creation is pinned to the Blessing AI project"
}
if ($ReleaseControllerServiceAccount -ne "blessing-release-controller@$ProjectId.iam.gserviceaccount.com") {
  throw "Release candidate creation requires the dedicated Release Controller identity"
}
if ($ControlPlaneUrl -notmatch '^https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])*$' -or
    $ControlPlaneUrl -notmatch '\.') {
  throw "ControlPlaneUrl must be a canonical HTTPS service URL"
}
if ($RepoSha -notmatch '^[0-9a-fA-F]{40}$' -and $RepoSha -notmatch '^[0-9a-fA-F]{64}$') {
  throw "RepoSha must be a commit SHA or SHA-256 digest"
}
if ($ImageUri -notmatch '@sha256:[0-9a-fA-F]{64}$') {
  throw "ImageUri must use an immutable registry digest"
}
if ($WorkerRevision -notmatch '^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$') {
  throw "WorkerRevision is invalid"
}

try {
  $expiry = [DateTimeOffset]::Parse($ExpiresAt)
} catch {
  throw "ExpiresAt must be a valid timestamp"
}
$now = [DateTimeOffset]::UtcNow
if ($expiry -le $now -or $expiry -gt $now.AddHours(24)) {
  throw "ExpiresAt must be in the future and no more than 24 hours away"
}

$activeAccounts = @( & gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>$null ) |
  ForEach-Object { ([string]$_).Trim() } |
  Where-Object { $_ }
if ($LASTEXITCODE -ne 0 -or $activeAccounts.Count -ne 1 -or $activeAccounts[0] -ne $ReleaseControllerServiceAccount) {
  throw "Active identity is not the dedicated Release Controller"
}

# The short-lived token is kept in memory only. It is never a parameter,
# logged value, request body field, or release-evidence field.
$identityToken = (& gcloud auth print-identity-token --audiences=$ControlPlaneUrl 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($identityToken)) {
  throw "Release Controller OIDC token could not be obtained"
}

$payload = [ordered]@{
  repoSha = $RepoSha
  imageDigest = $ImageUri
  workerRevision = $WorkerRevision
  secretVersions = [ordered]@{
    sql = $CloudSqlPasswordVersion
    apiKey = $BinanceMainnetApiKeyVersion
    apiSecret = $BinanceMainnetApiSecretVersion
  }
  preflightEvidenceHash = $PreflightEvidenceHash
  repoGateEvidenceHash = $RepoGateEvidenceHash
  cloudGateEvidenceHash = $CloudGateEvidenceHash
  expiresAt = $expiry.ToUniversalTime().ToString("o")
  nonce = $Nonce
}

$headers = @{ Authorization = "Bearer $identityToken" }
$response = Invoke-RestMethod `
  -Method Post `
  -Uri "$($ControlPlaneUrl.TrimEnd('/'))/internal/release/candidate" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body ($payload | ConvertTo-Json -Depth 5) `
  -TimeoutSec 90

if ([string]$response.status -ne "PENDING_APPROVAL" -or
    [string]$response.executionMode -ne "LIVE" -or
    [string]$response.symbol -ne "ETHUSDC" -or
    [int]$response.orderSubmissionAttempts -ne 0 -or
    $response.workerDisarmed -ne $true) {
  throw "Control Plane returned an invalid inactive release candidate"
}
if ([string]$response.candidateId -notmatch '^rc-[0-9a-fA-F-]{36}$') {
  throw "Control Plane returned an invalid release candidate id"
}

Write-Output "Release candidate created: $($response.candidateId)"
Write-Output "Status: $($response.status); symbol: $($response.symbol); policy: $($response.launchPolicy)"
Write-Output "Worker image digest and numeric secret versions were accepted; no ARM or order was sent"

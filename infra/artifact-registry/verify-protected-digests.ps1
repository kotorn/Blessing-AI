<#!
.SYNOPSIS
  Verify that release and rollback digests exist and remain tagged.

.DESCRIPTION
  This is a read-only precondition for any separately approved cleanup-policy
  change. It does not apply a policy and never deletes or retags an image.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Location = "asia-southeast1",
  [string]$Repository = "blessing-repo",
  [Parameter(Mandatory = $true)]
  [string[]]$ProtectedImageUri
)

$ErrorActionPreference = "Stop"
$package = "$Location-docker.pkg.dev/$ProjectId/$Repository"
$raw = & gcloud artifacts docker images list $package `
  --project=$ProjectId `
  --include-tags `
  --format=json
if ($LASTEXITCODE -ne 0) { throw "Unable to read Artifact Registry images" }

$images = @($raw | ConvertFrom-Json)
$missing = [System.Collections.Generic.List[string]]::new()
$untagged = [System.Collections.Generic.List[string]]::new()
foreach ($uri in $ProtectedImageUri) {
  if ($uri -notmatch '^.+@sha256:[0-9a-fA-F]{64}$') { throw "Protected image must use a full immutable digest URI: $uri" }
  $match = $images | Where-Object { "$($_.package)@$($_.version)" -eq $uri } | Select-Object -First 1
  if ($null -eq $match) {
    $missing.Add($uri)
    continue
  }
  if (@($match.tags).Count -eq 0) { $untagged.Add($uri) }
}

if ($missing.Count -gt 0) { throw "Protected image digests are missing: $($missing -join ', ')" }
if ($untagged.Count -gt 0) { throw "Protected image digests are untagged and are not safe to protect with the repository policy: $($untagged -join ', ')" }

Write-Output "Protected digest verification passed for $($ProtectedImageUri.Count) image(s); no cleanup action was applied"

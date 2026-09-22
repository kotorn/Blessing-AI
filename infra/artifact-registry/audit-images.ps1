<#!
.SYNOPSIS
  Read-only Artifact Registry image inventory for release evidence.

.DESCRIPTION
  Lists image versions, digests, tags, timestamps, and sizes. This script does
  not set a cleanup policy and never deletes an image.
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Location = "asia-southeast1",
  [string]$Repository = "blessing-repo",
  [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
if ($ProjectId -notmatch '^[a-z][a-z0-9-]{5,29}$') { throw "ProjectId is not a valid project identifier" }
if ($Location -notmatch '^[a-z0-9-]+$') { throw "Location is invalid" }
if ($Repository -notmatch '^[a-z0-9][a-z0-9._-]{0,62}$') { throw "Repository is invalid" }

$package = "$Location-docker.pkg.dev/$ProjectId/$Repository"
$raw = & gcloud artifacts docker images list $package `
  --project=$ProjectId `
  --include-tags `
  --format=json
if ($LASTEXITCODE -ne 0) { throw "Unable to read Artifact Registry images" }

$images = @($raw | ConvertFrom-Json | ForEach-Object {
  [ordered]@{
    package         = [string]$_.package
    digest          = [string]$_.version
    tags            = @($_.tags)
    create_time     = [string]$_.createTime
    update_time     = [string]$_.updateTime
    image_size_bytes = [int64]($_.imageSizeBytes ?? 0)
  }
})

$report = [ordered]@{
  project_id = $ProjectId
  location = $Location
  repository = $Repository
  generated_at = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
  destructive_action = $false
  image_count = $images.Count
  images = $images
}

$json = $report | ConvertTo-Json -Depth 10
if ($OutputPath) {
  $parent = Split-Path -Parent $OutputPath
  if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
  Set-Content -LiteralPath $OutputPath -Value $json -Encoding UTF8
}
Write-Output $json

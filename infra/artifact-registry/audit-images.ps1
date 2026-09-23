<#!
.SYNOPSIS
  Read-only Artifact Registry image inventory for release evidence.

.DESCRIPTION
  Lists image versions, digests, tags, timestamps, and sizes, and flags
  versions that are older than -OlderThanDays and carry no protected tag
  (and are not listed in -ProtectedImageUri) as cleanup candidates.

  This script never sets a cleanup policy and never deletes an image.

.EXAMPLE
  powershell -File audit-images.ps1 -OlderThanDays 30 -ProtectedTagPatterns 'v*'

.EXAMPLE
  powershell -File audit-images.ps1 -ProtectedImageUri @(
    'asia-southeast1-docker.pkg.dev/gen-lang-client-0730128480/blessing-repo/control-plane@sha256:206af16139b38b641164e7c746905d4bfce3fae14fff9b5aee2093130f3b614d'
  )
#>
param(
  [string]$ProjectId = "gen-lang-client-0730128480",
  [string]$Location = "asia-southeast1",
  [string]$Repository = "blessing-repo",
  [int]$OlderThanDays = 30,
  [string[]]$ProtectedTagPatterns = @("v*"),
  [string[]]$ProtectedImageUri = @(),
  [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
if ($ProjectId -notmatch '^[a-z][a-z0-9-]{5,29}$') { throw "ProjectId is not a valid project identifier" }
if ($Location -notmatch '^[a-z0-9-]+$') { throw "Location is invalid" }
if ($Repository -notmatch '^[a-z0-9][a-z0-9._-]{0,62}$') { throw "Repository is invalid" }
if ($OlderThanDays -lt 1) { throw "OlderThanDays must be a positive number of days" }

$package = "$Location-docker.pkg.dev/$ProjectId/$Repository"
$raw = & gcloud artifacts docker images list $package `
  --project=$ProjectId `
  --include-tags `
  --format=json
if ($LASTEXITCODE -ne 0) { throw "Unable to read Artifact Registry images" }

# gcloud >= 440 prints a human preamble ("Listing items under ...") before the
# JSON body on stdout. Keep only from the first JSON-looking line onward.
$jsonLines = @()
$started = $false
foreach ($line in @($raw)) {
  if (-not $started -and $line -match '^\s*[\[{]') { $started = $true }
  if ($started) { $jsonLines += $line }
}
if ($jsonLines.Count -eq 0) { throw "Artifact Registry returned no JSON body to parse" }
# ConvertFrom-Json -InputObject (not the pipeline) so Windows PowerShell 5.1
# does not wrap the parsed array inside a single-element array.
$apiImages = @(ConvertFrom-Json -InputObject ($jsonLines -join "`n"))

$now = [DateTimeOffset]::UtcNow
$images = @()
$candidates = 0
foreach ($entry in $apiImages) {
  $tags = @()
  if ($null -ne $entry.tags) { $tags = @($entry.tags | Where-Object { $null -ne $_ -and "$_" -ne "" }) }
  # Windows PowerShell 5.1 ConvertFrom-Json coerces ISO-8601 strings to
  # [DateTime] (then culture-dependent when stringified); PowerShell 7 keeps
  # strings. Normalize both into a UTC DateTimeOffset.
  $createTime = $null
  if ($entry.createTime -is [datetime]) {
    $createTime = [DateTimeOffset]::new($entry.createTime.ToUniversalTime())
  } else {
    $createTime = [DateTimeOffset]::Parse([string]$entry.createTime, [System.Globalization.CultureInfo]::InvariantCulture)
  }
  $ageDays = [int][Math]::Floor(($now - $createTime).TotalDays)

  $sizeBytes = 0
  if ($null -ne $entry.metadata -and $null -ne $entry.metadata.imageSizeBytes) {
    $sizeBytes = [int64]$entry.metadata.imageSizeBytes
  }

  $matchedProtectedTags = @($tags | Where-Object {
    $tag = $_
    @($ProtectedTagPatterns | Where-Object { $tag -like $_ }).Count -gt 0
  })
  $digestUri = "$($entry.package)@$($entry.version)"
  $isProtectedByUri = @($ProtectedImageUri | Where-Object { $_ -eq $digestUri }).Count -gt 0
  $isProtected = ($matchedProtectedTags.Count -gt 0) -or $isProtectedByUri
  $isOld = $ageDays -ge $OlderThanDays
  $flagged = ($isOld -and -not $isProtected)
  if ($flagged) { $candidates++ }

  $images += [ordered]@{
    package          = [string]$entry.package
    digest           = [string]$entry.version
    tags             = $tags
    create_time      = $createTime.UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ssZ")
    age_days         = $ageDays
    image_size_bytes = $sizeBytes
    protected        = $isProtected
    protected_reason = if ($isProtectedByUri) { "digest-allowlist" } elseif ($matchedProtectedTags.Count -gt 0) { "tag-pattern: $($matchedProtectedTags -join ', ')" } else { "" }
    flagged_old_unprotected = $flagged
  }
}

$flaggedImages = @($images | Where-Object { $_.flagged_old_unprotected })
$totalBytes = ($images | Measure-Object -Property image_size_bytes -Sum).Sum
if ($null -eq $totalBytes) { $totalBytes = 0 }

$report = [ordered]@{
  project_id         = $ProjectId
  location           = $Location
  repository         = $Repository
  generated_at       = $now.UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ssZ")
  destructive_action = $false
  older_than_days    = $OlderThanDays
  protected_tag_patterns = $ProtectedTagPatterns
  protected_image_uris = $ProtectedImageUri
  image_count        = $images.Count
  untagged_count     = @($images | Where-Object { $_.tags.Count -eq 0 }).Count
  protected_count    = @($images | Where-Object { $_.protected }).Count
  flagged_old_unprotected_count = $flaggedImages.Count
  total_image_size_bytes = [int64]$totalBytes
  images             = $images
}

$json = $report | ConvertTo-Json -Depth 10
if ($OutputPath) {
  $parent = Split-Path -Parent $OutputPath
  if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
  Set-Content -LiteralPath $OutputPath -Value $json -Encoding UTF8
}

Write-Output ("Artifact Registry audit (read-only): {0} image(s) in {1}" -f $images.Count, $package)
Write-Output ("Protected: {0}; untagged: {1}; flagged older than {2}d without protected tag: {3}" -f $report.protected_count, $report.untagged_count, $OlderThanDays, $flaggedImages.Count)
foreach ($flag in $flaggedImages) {
  Write-Output ("CLEANUP-CANDIDATE: {0}@{1} age={2}d tags=[{3}]" -f $flag.package, $flag.digest, $flag.age_days, ($flag.tags -join ","))
}
Write-Output "No cleanup policy was set and no image was deleted by this script."
Write-Output $json

<#
.SYNOPSIS
  Verify that the public Control Plane rejects unauthenticated requests.

.DESCRIPTION
  The Control Plane may expose public HTTPS transport for the SPA, but every
  protected API must reject anonymous and invalid Firebase identities before
  reaching route logic. This probe deliberately uses no valid user token and
  never prints response bodies or authorization material.
#>
param(
  [Parameter(Mandatory = $true)]
  [string]$ControlPlaneUrl
)

$ErrorActionPreference = "Stop"

if ($ControlPlaneUrl -notmatch '^https://[^/?:#]+$') {
  throw "ControlPlaneUrl must be a canonical HTTPS service URL"
}

function Get-RejectedStatus {
  param(
    [Parameter(Mandatory = $true)][ValidateSet("GET", "POST")][string]$Method,
    [Parameter(Mandatory = $true)][string]$Path,
    [hashtable]$Headers = @{},
    [string]$Body = ""
  )

  $requestUri = "$($ControlPlaneUrl.TrimEnd('/'))$Path"
  try {
    $requestArgs = @{
      Method = $Method
      Uri = $requestUri
      Headers = $Headers
      TimeoutSec = 30
      MaximumRedirection = 0
      UseBasicParsing = $true
    }
    if ($Method -eq "POST") {
      $requestArgs.ContentType = "application/json"
      $requestArgs.Body = $Body
    }
    $response = Invoke-WebRequest @requestArgs
    return [int]$response.StatusCode
  } catch {
    $response = $_.Exception.Response
    if ($null -eq $response) {
      throw "Control Plane auth probe failed without an HTTP response for $Path"
    }
    try {
      return [int]$response.StatusCode.value__
    } catch {
      return [int]$response.StatusCode
    }
  }
}

function Assert-AuthRejected {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][ValidateSet("GET", "POST")][string]$Method,
    [Parameter(Mandatory = $true)][string]$Path,
    [hashtable]$Headers = @{},
    [string]$Body = ""
  )
  $status = Get-RejectedStatus -Method $Method -Path $Path -Headers $Headers -Body $Body
  if ($status -notin @(401, 403)) {
    throw "$Name was not rejected before route logic (HTTP $status)"
  }
  Write-Output "$Name rejected as expected (HTTP $status)"
}

Assert-AuthRejected -Name "Anonymous protected state" -Method "GET" -Path "/api/system/state"
Assert-AuthRejected -Name "Invalid Firebase identity" -Method "GET" -Path "/api/system/state" `
  -Headers @{ Authorization = "Bearer invalid-firebase-id-token" }
Assert-AuthRejected -Name "Anonymous ARM mutation" -Method "POST" -Path "/api/system/arm" `
  -Body '{"executionMode":"PAPER"}'
Assert-AuthRejected -Name "Anonymous release approval" -Method "POST" -Path "/api/release/mainnet/approve" `
  -Body '{"candidateId":"rc-00000000-0000-0000-0000-000000000000"}'
Assert-AuthRejected -Name "Anonymous internal release identity" -Method "POST" -Path "/internal/release/runtime" `
  -Body '{}'
Assert-AuthRejected -Name "Anonymous internal release readiness" -Method "POST" -Path "/internal/release/readiness" `
  -Body '{}'
Assert-AuthRejected -Name "Invalid Google service identity" -Method "POST" -Path "/internal/release/readiness" `
  -Headers @{ Authorization = "Bearer invalid-google-oidc-token" } `
  -Body '{}'

Write-Output "Control Plane anonymous/invalid identity denial verified without valid credentials"

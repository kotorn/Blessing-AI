$ErrorActionPreference = "Stop"

$cloudSdkImage = "gcr.io/google.com/cloudsdktool/google-cloud-cli:slim@sha256:0673a69a1e178abb079941b326e718aedf3f57bdf5343465e365d826472edd83"
$controllerScriptSha = "e08047f4441ba728a4cdc1c57a0357fed3691d760f08d777882c0ef28058417b"
$controlPlaneUrl = "https://blessing-control-plane-hrybwxl4ra-as.a.run.app"
$candidateId = "rc-a6f105e5-a3b0-4a01-aee1-2b5a1f81ca14"
$imageUri = "asia-southeast1-docker.pkg.dev/gen-lang-client-0730128480/blessing-repo/trading-worker@sha256:5e92cad8e749c1197b7decc29b8881d482b860cb06890baee7d2c73fe337d0fc"
$projectId = "gen-lang-client-0730128480"
$region = "asia-southeast1"
$cloudSqlPasswordVersion = "1"
$binanceApiKeyVersion = "2"
$binanceApiSecretVersion = "2"
$releaseControllerSa = "blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com"

$subs = @(
    "_CLOUD_SDK_IMAGE=$cloudSdkImage",
    "_RELEASE_CONTROLLER_SCRIPT_SHA256=$controllerScriptSha",
    "_CONTROL_PLANE_URL=$controlPlaneUrl",
    "_CANDIDATE_ID=$candidateId",
    "_IMAGE_URI=$imageUri",
    "_PROJECT_ID=$projectId",
    "_REGION=$region",
    "_CLOUD_SQL_PASSWORD_VERSION=$cloudSqlPasswordVersion",
    "_BINANCE_API_KEY_VERSION=$binanceApiKeyVersion",
    "_BINANCE_API_SECRET_VERSION=$binanceApiSecretVersion",
    "_RELEASE_CONTROLLER_SERVICE_ACCOUNT=$releaseControllerSa"
) -join ","

Write-Output "Submitting Cloud Build with candidateId: $candidateId"
& gcloud builds submit `
    --config="cloudbuild-release-controller.yaml" `
    --project="$projectId" `
    "--substitutions=$subs"

if ($LASTEXITCODE -ne 0) {
    throw "Cloud Build submission failed with code $LASTEXITCODE"
}
Write-Output "Cloud Build submission succeeded"

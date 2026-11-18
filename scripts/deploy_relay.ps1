<#
.SYNOPSIS
Deploys the Biruni Cloudflare Worker relay from relay\.

Required Cloudflare API token permissions:
- Account · Workers Scripts · Edit
- Zone biruni-gis.com · Workers Routes · Edit
- Zone · Zone · Read
- Account Settings · Read (quiets wrangler)
- User Details · Read (quiets wrangler)

Required environment variables:
- CLOUDFLARE_API_TOKEN
- CLOUDFLARE_ACCOUNT_ID
#>
param(
    [switch]$DryRun
)

if ([string]::IsNullOrWhiteSpace($env:CLOUDFLARE_API_TOKEN) -or
    [string]::IsNullOrWhiteSpace($env:CLOUDFLARE_ACCOUNT_ID)) {
    Write-Error "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID must be set."
    exit 1
}

$relayDir = Join-Path (Split-Path -Parent $PSScriptRoot) "relay"
Push-Location $relayDir
try {
    if ($DryRun) {
        npx --yes wrangler@4 deploy --dry-run --outdir ..\build\relay
    } else {
        npx --yes wrangler@4 deploy
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

<#
.SYNOPSIS
  Deploys PDF AI Assistant to Azure Container Apps, end to end.

.DESCRIPTION
  1. Creates the vault, identity, logs and Container Apps environment (infra/main.bicep).
  2. Stops if the Claude API key isn't in the vault yet, and tells you how to add it.
  3. Lets the app's managed identity into the Azure SQL database (no password).
  4. Deploys the app, then turns on Microsoft sign-in for it.
  Safe to run again: every step checks what already exists.

.EXAMPLE
  ./infra/deploy.ps1 -ResourceGroup rg-pdf-ai -SqlServer sql-claude-moni-polandcentral `
      -SqlDatabase appdb -Image ghcr.io/momchilborisov05-eng/pdf-ai-chatbot:latest `
      -AllowedUsers "me@example.com"
#>
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $SqlServer,      # server name, without .database.windows.net
    [Parameter(Mandatory)] [string] $SqlDatabase,
    [Parameter(Mandatory)] [string] $Image,
    [string] $Location = "polandcentral",
    [string] $AppName = "pdf-ai",
    [string] $AllowedUsers = "",
    [string] $DailyBudgetUsd = "1.00"
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$sqlHost = "$SqlServer.database.windows.net"

function Invoke-Az {
    $out = az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    return $out
}

function Deploy-Infra([bool] $deployApp) {
    $result = Invoke-Az deployment group create -g $ResourceGroup -f "$PSScriptRoot/main.bicep" `
        --parameters image=$Image sqlServerHost=$sqlHost sqlDatabase=$SqlDatabase appName=$AppName `
                     allowedUsers=$AllowedUsers dailyBudgetUsd=$DailyBudgetUsd `
                     deployerObjectId=$me deployApp=$($deployApp.ToString().ToLower()) `
        --query properties.outputs -o json
    return $result | ConvertFrom-Json
}

Write-Host "==> Resource group $ResourceGroup ($Location)"
Invoke-Az group create -n $ResourceGroup -l $Location -o none
$me = Invoke-Az ad signed-in-user show --query id -o tsv
$tenant = Invoke-Az account show --query tenantId -o tsv

Write-Host "==> Identity, Key Vault, logs, Container Apps environment"
$out = Deploy-Infra $false
$vault = $out.keyVaultName.value
$identity = $out.identityName.value

$secret = az keyvault secret list --vault-name $vault --query "[?name=='anthropic-api-key'].id" -o tsv
if (-not $secret) {
    Write-Host ""
    Write-Host "The Claude API key is not in Key Vault '$vault' yet. Add it yourself, then run this script again:" -ForegroundColor Yellow
    Write-Host "  az keyvault secret set --vault-name $vault --name anthropic-api-key --value (Read-Host 'API key') -o none"
    exit 1
}

Write-Host "==> Azure SQL: allow Azure services and grant the app's identity access"
Invoke-Az sql server firewall-rule create -g (Invoke-Az sql server list --query "[?name=='$SqlServer'].resourceGroup | [0]" -o tsv) `
    -s $SqlServer -n AllowAzureServices --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 -o none
& "$root/.venv/Scripts/python.exe" "$PSScriptRoot/grant_sql_access.py" $sqlHost $SqlDatabase $identity
if ($LASTEXITCODE -ne 0) { throw "Granting database access failed" }

Write-Host "==> Container app"
$out = Deploy-Infra $true
$url = $out.appUrl.value
$appName = $out.appName.value

Write-Host "==> Microsoft sign-in"
$registration = "$AppName-signin"
$clientId = az ad app list --display-name $registration --query "[0].appId" -o tsv
if (-not $clientId) {
    $clientId = Invoke-Az ad app create --display-name $registration --sign-in-audience AzureADMyOrg `
        --web-redirect-uris "$url/.auth/login/aad/callback" --enable-id-token-issuance true --query appId -o tsv
}
$clientSecret = Invoke-Az ad app credential reset --id $clientId --display-name "container-apps" --years 1 --query password -o tsv
Invoke-Az containerapp auth microsoft update -n $appName -g $ResourceGroup --client-id $clientId `
    --client-secret $clientSecret --tenant-id $tenant --yes -o none
Invoke-Az containerapp auth update -n $appName -g $ResourceGroup --unauthenticated-client-action RedirectToLoginPage `
    --redirect-provider azureactivedirectory --excluded-paths "/healthz,/readyz" -o none

Write-Host ""
Write-Host "Deployed: $url" -ForegroundColor Green
Write-Host "Readiness: $url/readyz"

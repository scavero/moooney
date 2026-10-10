# Prueba de Clef-Flash a traves del proxy LiteLLM.
# Uso (en PowerShell, desde la carpeta del proyecto):
#   $env:LITELLM_KEY = "sk-..."      # tu clave del proxy, no la pegues en el chat
#   .\docs\clef_test.ps1

$key = $env:LITELLM_API_KEY
if (-not $key) { $key = $env:LITELLM_KEY }
if (-not $key) { $key = [Environment]::GetEnvironmentVariable("LITELLM_API_KEY", "User") }
if (-not $key) {
    Write-Error "Define primero la variable de entorno LITELLM_API_KEY (setx LITELLM_API_KEY sk-...)."
    exit 1
}

$body = Get-Content -Raw -Path (Join-Path $PSScriptRoot "clef_test.json")

$response = Invoke-RestMethod `
    -Method Post `
    -Uri "https://litellm.clusterticgalicia.com/v1/chat/completions" `
    -Headers @{ Authorization = "Bearer $key" } `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($body))

$response | ConvertTo-Json -Depth 20


$ErrorActionPreference = "Stop"

$secretsFile = Join-Path $PSScriptRoot "local-secrets.ps1"
if (Test-Path -LiteralPath $secretsFile) {
    . $secretsFile
}

if (-not $env:MODEL_API_KEY) {
    $secureKey = Read-Host "Enter the rotated Azure Foundry API key" -AsSecureString
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try {
        $env:MODEL_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
}

$env:AGENT_MODEL_PROVIDER = "azure-foundry-responses"
$env:MODEL_ENDPOINT = "https://steveaihackthon0829.services.ai.azure.com/openai/v1/responses"
$env:MODEL_NAME = "gpt-5.6-sol"

python "$PSScriptRoot\server.py"

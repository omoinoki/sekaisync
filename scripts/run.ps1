[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
$env:PYTHONPYCACHEPREFIX = Join-Path $root 'build\pycache'
Push-Location $root
try {
    & $py -m sekaisync @Arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

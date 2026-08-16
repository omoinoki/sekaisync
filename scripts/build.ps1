[CmdletBinding()]
param(
    [string]$OutputDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist')
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
$env:PYTHONPYCACHEPREFIX = Join-Path $root 'build\pycache'
Push-Location $root
try {
    & $py -m pip wheel . --no-deps --no-build-isolation --wheel-dir $OutputDir
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

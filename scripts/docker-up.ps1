[CmdletBinding()]
param(
    [switch]$Detached,
    [switch]$NoCache
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker Desktop is required. Install and start Docker Desktop, then run this script again.'
}

Push-Location $projectRoot
try {
    $composeArgs = @('compose', 'up', '--build', '--remove-orphans')
    if ($NoCache) {
        & docker compose build --no-cache
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        $composeArgs = @('compose', 'up', '--remove-orphans')
    }
    if ($Detached) {
        $composeArgs += '--detach'
    }
    & docker @composeArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

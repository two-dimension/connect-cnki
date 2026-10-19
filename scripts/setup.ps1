param([switch]$InstallBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    uv sync --frozen --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    if (-not (Test-Path -LiteralPath 'config.local.json')) {
        Copy-Item -LiteralPath 'config.example.json' -Destination 'config.local.json'
    }
    New-Item -ItemType Directory -Force -Path 'imports' | Out-Null
    if ($InstallBrowser) {
        uv run playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw 'Browser installation failed.' }
    }
    uv run cnki-mcp --config config.local.json --check
    if ($LASTEXITCODE -ne 0) { throw 'Configuration validation failed.' }
} finally {
    Pop-Location
}

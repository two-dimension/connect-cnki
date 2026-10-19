param(
    [switch]$InstallBrowser,
    [ValidateSet('msedge', 'chrome', 'chromium')][string]$BrowserChannel = 'msedge'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    uv sync --frozen --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    npm.cmd ci --prefix src/cnki_mcp/js --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'CSL renderer installation failed.' }
    if (-not (Test-Path -LiteralPath 'config.local.json')) {
        $localConfig = Get-Content -LiteralPath 'config.example.json' -Raw -Encoding UTF8 | ConvertFrom-Json
        $localConfig.browser_channel = $BrowserChannel
        $localConfig | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath 'config.local.json' -Encoding utf8NoBOM
    }
    New-Item -ItemType Directory -Force -Path 'imports' | Out-Null
    if ($InstallBrowser) {
        $browserDataPath = uv run python -c 'from cnki_mcp.config import Settings; print(Settings.load("config.local.json").data_dir / "browsers")'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve browser installation directory.' }
        $env:PLAYWRIGHT_BROWSERS_PATH = $browserDataPath
        uv run playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw 'Browser installation failed.' }
    }
    uv run cnki-mcp --config config.local.json --check
    if ($LASTEXITCODE -ne 0) { throw 'Configuration validation failed.' }
} finally {
    Pop-Location
}

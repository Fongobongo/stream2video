param(
    [switch]$Dev
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Write-Ok($msg) {
    Write-Host "  [+] $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "  [!] $msg" -ForegroundColor Yellow
}

function Test-Command($cmd) {
    return [bool](Get-Command $cmd -ErrorAction SilentlyContinue)
}

# --- Python deps ---
Write-Step "Installing Python dependencies..."
$deps = "pip install -e `"$PSScriptRoot`""
if ($Dev) {
    $deps += "`"[dev]`""
}
Invoke-Expression $deps
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Write-Ok "Python dependencies installed"

# --- ffmpeg ---
Write-Step "Checking ffmpeg..."
if (Test-Command ffmpeg) {
    Write-Ok "ffmpeg found: $(ffmpeg -version 2>&1 | Select-Object -First 1)"
} else {
    Write-Warn "ffmpeg not found. Installing via winget..."
    winget install --id Gyan.FFmpeg --source winget --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
    if (-not (Test-Command ffmpeg)) {
        throw "ffmpeg installation failed. Install manually: winget install Gyan.FFmpeg"
    }
    Write-Ok "ffmpeg installed"
}

Write-Step "Setup complete!"
Write-Host "Run: stream2video <url_or_file> --output ./compressed_videos" -ForegroundColor Cyan

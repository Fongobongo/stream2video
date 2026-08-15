param(
    [switch]$Gui,
    [switch]$Dev,
    [switch]$Update,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [+] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [!] $msg" -ForegroundColor Yellow }
function Test-Command($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# --- paths ---
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$GuiFlag = $Gui

# --- Install / Update Python deps ---
if ((-not $Args) -or $Update -or $Gui) {
    Write-Step "Checking Python dependencies..."
    $pkg = $ScriptDir
    if ($Dev) { $pkg += "[dev]" }

    & pip install -e $pkg
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

    # GUI extras (optional). Must match run_gui.cmd's ".[gui,monitor]":
    # the GUI imports customtkinter + PIL (waveform rendering) + psutil
    # (memory monitor) at startup — installing bare customtkinter used
    # to pass the guard and then crash the GUI on the PIL import
    # (R4.5 audit: the two installers produced different environments).
    if ($Gui -and -not (python -c "import customtkinter, PIL, psutil" 2>$null)) {
        $guiPkg = if ($Dev) { "$ScriptDir[dev,gui,monitor]" } else { "$ScriptDir[gui,monitor]" }
        pip install -e $guiPkg --quiet 2>&1 | Out-Null
    }
    Write-Ok "Python dependencies installed"
}

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

# --- Launch ---
if ($Gui) {
    Write-Step "Launching GUI..."
    python -m stream2video.gui
    exit $LASTEXITCODE
} elseif ($Args) {
    Write-Step "Running: stream2video $($Args -join ' ')"
    python -m stream2video.cli $Args
    exit $LASTEXITCODE
} else {
    Write-Step "Setup complete!"
    Write-Host ""
    Write-Host "Usage:" -ForegroundColor Cyan
    Write-Host "  .\setup.ps1                         -- install / update deps" -ForegroundColor White
    Write-Host "  .\setup.ps1 -Gui                    -- launch GUI" -ForegroundColor White
    Write-Host "  .\setup.ps1 video.mp4                -- compress video" -ForegroundColor White
    Write-Host "  .\setup.ps1 -Dev                     -- install with dev deps (tests)" -ForegroundColor White
    Write-Host ""
    Write-Host "Examples:" -ForegroundColor Cyan
    Write-Host "  .\setup.ps1 -Gui" -ForegroundColor Green
    Write-Host "  .\setup.ps1 video.mp4 -m batch -e libx264" -ForegroundColor Green
    Write-Host "  .\setup.ps1 https://youtube.com/watch?v=..." -ForegroundColor Green
}

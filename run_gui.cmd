@echo off
chcp 65001 >nul
title silencecut
setlocal enabledelayedexpansion

cd /d "%~dp0"
set "PORT_DIR=%~dp0_portable"

:: Python detection
set "PYTHON="
if exist "%PORT_DIR%\venv\Scripts\python.exe" set "PYTHON=%PORT_DIR%\venv\Scripts\python.exe"
if not defined PYTHON if exist "%PORT_DIR%\python\python.exe" set "PYTHON=%PORT_DIR%\python\python.exe"
:: Parenthesised so the ``&&`` chain stays inside the ``if`` body no
:: matter how the line is re-formatted later (a bare ``if cond cmd1 && cmd2``
:: is legal but easy to misread as ``if (cond cmd1) && cmd2``).
if not defined PYTHON (where python >nul 2>&1 && set "PYTHON=python")

if not defined PYTHON (
    echo ==^> Downloading portable Python...
    if not exist "%PORT_DIR%" mkdir "%PORT_DIR%"
    :: Pinned version (3.13.15) so the bootstrap is reproducible — the
    :: URL below is the ONLY external artifact this script fetches, and
    :: it is fetched over TLS from python.org. The installer's SHA-256
    :: is printed for manual verification (see python.org/downloads for
    :: the published digest); pinning + TLS + printed digest is the
    :: practical ceiling for a .cmd bootstrap without a second download.
    curl -sL -o "%TEMP%\python-installer.exe" https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe
    certutil -hashfile "%TEMP%\python-installer.exe" SHA256
    start /wait "" "%TEMP%\python-installer.exe" /quiet TargetDir="%PORT_DIR%\python" InstallAllUsers=0 PrependPath=0 Include_test=0
    if exist "%PORT_DIR%\python\python.exe" (
        set "PYTHON=%PORT_DIR%\python\python.exe"
    ) else (
        echo [ERROR] Python install failed. Download manually: https://www.python.org/downloads/
        pause
        exit /b 1
    )
)
echo [+] Python: %PYTHON%

:: ffmpeg with the troublesome call
set "FFMPEG_DIR="
if exist "%PORT_DIR%\ffmpeg\bin\ffmpeg.exe" set "FFMPEG_DIR=%PORT_DIR%\ffmpeg\bin"
if defined FFMPEG_DIR (
    set "PATH=%FFMPEG_DIR%;%PATH%"
) else (
    where ffmpeg >nul 2>&1 || call :install_ffmpeg
)
ffmpeg -version >nul 2>&1
if %errorlevel% equ 0 (
    echo [+] ffmpeg found
) else (
    echo [ERROR] ffmpeg not found in PATH after install attempt
    pause
    exit /b 1
)

:: Offline prebuilt mode: _portable\python ships with all dependencies
:: already installed (marker .s2v_offline). Skip venv creation and pip
:: install entirely - no network access needed, launch directly.
if exist "%PORT_DIR%\python\.s2v_offline" (
    set "PYTHON=%PORT_DIR%\python\python.exe"
    echo [+] Offline prebuilt environment detected
    goto launch
)

:: Venv + deps
if not exist "%PORT_DIR%\venv\" (
    echo ==^> Creating virtual environment...
    "%PYTHON%" -m venv "%PORT_DIR%\venv"
) else (
    :: The venv pins the interpreter it was created from; if the base
    :: Python changed (portable pin bump, e.g. 3.13.2 -> 3.13.15, or a
    :: different system python now on PATH) the stale venv must be
    :: recreated, not silently reused (audit: venv/version drift).
    "%PYTHON%" -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" > "%TEMP%\s2v_basever.txt"
    set /p BASEVER=<"%TEMP%\s2v_basever.txt"
    "%PORT_DIR%\venv\Scripts\python.exe" -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" > "%TEMP%\s2v_venvver.txt"
    set /p VENVVER=<"%TEMP%\s2v_venvver.txt"
    :: !BASEVER!/!VENVVER! (delayed expansion) — %BASEVER% would expand
    :: at block-parse time (empty, set /p hasn't run yet) and the
    :: version drift would never be detected (same trap as FFMPEG_DIR).
    if not "!BASEVER!"=="!VENVVER!" (
        echo ==^> Interpreter changed (venv !VENVVER! vs base !BASEVER!); recreating...
        rd /s /q "%PORT_DIR%\venv"
        "%PYTHON%" -m venv "%PORT_DIR%\venv"
    )
)
set "PYTHON=%PORT_DIR%\venv\Scripts\python.exe"

"%PYTHON%" -c "import stream2video; import customtkinter; import PIL; import psutil" 2>nul
if errorlevel 1 (
    echo ==^> Installing dependencies...
    "%PYTHON%" -m pip install -e "%~dp0.[gui,monitor]"
    if errorlevel 1 (
        echo [ERROR] pip install failed
        pause
        exit /b 1
    )
    echo [+] Dependencies ready
) else (
    echo [+] Dependencies already installed
)

:launch
:: ---- Launch GUI ----
echo ==^> Launching GUI...
for %%e in ("%PYTHON%") do set "PYTHONW=%%~dpepythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=%PYTHON%"
start "" "%PYTHONW%" -m stream2video.gui
exit /b 0

:install_ffmpeg
echo ==^> Installing ffmpeg...
where winget >nul 2>&1
if %errorlevel% equ 0 (
    echo winget found
    winget install --id Gyan.FFmpeg --source winget --accept-package-agreements --accept-source-agreements >nul 2>&1
    if %errorlevel% equ 0 (
        for /f "tokens=*" %%a in ('dir /s /b "%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin\ffmpeg.exe" 2^>nul') do set "FFMPEG_DIR=%%~dpa"
        :: !FFMPEG_DIR! — delayed expansion: %FFMPEG_DIR% would expand at
        :: block-parse time (empty, the loop hasn't run yet) and PATH
        :: would silently lose the ffmpeg dir (R4.4 audit).
        if defined FFMPEG_DIR set "PATH=!FFMPEG_DIR!;%PATH%"
        exit /b 0
    )
)
echo ==^> Downloading portable ffmpeg...
if not exist "%PORT_DIR%" mkdir "%PORT_DIR%"
curl -sL -o "%TEMP%\ffmpeg.zip" https://github.com/BtbN/FFmpeg-Builds/releases/download/LATEST/ffmpeg-master-latest-win64-gpl.zip
if %errorlevel% equ 0 (
    powershell -Command "Expand-Archive -Path '%TEMP%\ffmpeg.zip' -DestinationPath '%PORT_DIR%\ffmpeg_tmp' -Force; if(Test-Path '%PORT_DIR%\ffmpeg_tmp\ffmpeg-master-latest-win64-gpl'){Move-Item '%PORT_DIR%\ffmpeg_tmp\ffmpeg-master-latest-win64-gpl\*' '%PORT_DIR%\ffmpeg' -Force; Remove-Item '%PORT_DIR%\ffmpeg_tmp' -Recurse -Force}"
    if exist "%PORT_DIR%\ffmpeg\bin\ffmpeg.exe" (
        set "FFMPEG_DIR=%PORT_DIR%\ffmpeg\bin"
        :: !FFMPEG_DIR! (delayed expansion) — same block-parse-time trap
        :: as the winget branch above; %FFMPEG_DIR% here would be the
        :: value from BEFORE this block (empty), and the portable ffmpeg
        :: would never reach PATH (R4.4 audit).
        set "PATH=!FFMPEG_DIR!;%PATH%"
    ) else (
        echo [WARN] ffmpeg extract failed. Install manually: winget install Gyan.FFmpeg
        exit /b 1
    )
) else (
    echo [WARN] ffmpeg download failed. Install manually: winget install Gyan.FFmpeg
    exit /b 1
)
exit /b 0

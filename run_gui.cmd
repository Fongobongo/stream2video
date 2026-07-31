@echo off
chcp 65001 >nul
title stream2video
setlocal enabledelayedexpansion

cd /d "%~dp0"
set "PORT_DIR=%~dp0_portable"

:: Python detection
set "PYTHON="
if exist "%PORT_DIR%\venv\Scripts\python.exe" set "PYTHON=%PORT_DIR%\venv\Scripts\python.exe"
if not defined PYTHON if exist "%PORT_DIR%\python\python.exe" set "PYTHON=%PORT_DIR%\python\python.exe"
if not defined PYTHON where python >nul 2>&1 && set "PYTHON=python"

if not defined PYTHON (
    echo ==^> Downloading portable Python...
    if not exist "%PORT_DIR%" mkdir "%PORT_DIR%"
    curl -sL -o "%TEMP%\python-installer.exe" https://www.python.org/ftp/python/3.13.2/python-3.13.2-amd64.exe
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

:: Venv + deps
if not exist "%PORT_DIR%\venv\" (
    echo ==^> Creating virtual environment...
    "%PYTHON%" -m venv "%PORT_DIR%\venv"
)
set "PYTHON=%PORT_DIR%\venv\Scripts\python.exe"

"%PYTHON%" -c "import stream2video; import customtkinter; import PIL; import psutil" 2>nul
if errorlevel 1 (
    echo ==^> Installing dependencies...
    "%PYTHON%" -m pip install -e ".[gui,monitor]"
    if errorlevel 1 (
        echo [ERROR] pip install failed
        pause
        exit /b 1
    )
    echo [+] Dependencies ready
) else (
    echo [+] Dependencies already installed
)

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
        if defined FFMPEG_DIR set "PATH=%FFMPEG_DIR%;%PATH%"
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
        set "PATH=%FFMPEG_DIR%;%PATH%"
    ) else (
        echo [WARN] ffmpeg extract failed. Install manually: winget install Gyan.FFmpeg
        exit /b 1
    )
) else (
    echo [WARN] ffmpeg download failed. Install manually: winget install Gyan.FFmpeg
    exit /b 1
)
exit /b 0

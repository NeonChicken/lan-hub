@echo off
rem Fallback starter (shows a console window). Normally just double-click "LAN Hub.exe" one folder up.
setlocal
rem never run programs from the current folder by accident
set "NoDefaultCurrentDirectoryInExePath=1"
title LAN Hub
cd /d "%~dp0"

set "PY="
if exist "runtime\python\python.exe" set "PY=runtime\python\python.exe"
if not defined PY (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=python"
)

if not defined PY (
  echo.
  echo   LAN Hub needs Python 3, and the bundled copy in app\runtime is missing.
  echo   It can download the official portable Python 3.12.8 from python.org ^(about 11 MB^).
  echo.
  "%SystemRoot%\System32\choice.exe" /C YN /M "  Download it now"
  if errorlevel 2 goto :nopy
  "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; $z=Join-Path $env:TEMP ('lanhub-python-' + [guid]::NewGuid() + '.zip'); Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip' -OutFile $z -UseBasicParsing; if ((Get-FileHash $z -Algorithm SHA256).Hash -ne '8D3F33BE9EB810F23C102F08475AF2854E50484B8E4E06275E937BE61CE3D2FB') { Remove-Item $z; throw 'Download checksum mismatch, refusing to use it.' }; Expand-Archive $z -DestinationPath 'runtime\python' -Force; Remove-Item $z"
  if exist "runtime\python\python.exe" set "PY=runtime\python\python.exe"
)
if not defined PY goto :nopy

rem install a downloaded + verified update first (only does something when one is waiting)
if exist "data\update\ready.json" %PY% -I tools\updater.py --apply
%PY% -I tools\lanhub.py %*
if errorlevel 1 pause
exit /b

:nopy
echo.
echo   Python is required. Get it from https://www.python.org/downloads/ (tick "Add to PATH").
echo.
pause
exit /b 1

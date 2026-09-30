@echo off
chcp 65001 >nul
cd /d "%~dp0server"

set "PY=python"
where python >nul 2>&1 || set "PY=py"

echo Stopping old Wi-Fi Remote servers...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%%' or Name like 'py.exe'\" | Where-Object { $_.CommandLine -like '*pc_server*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul

rem Firewall rules (works only when this file is run as administrator; otherwise use allow_firewall.bat once)
netsh advfirewall firewall add rule name="WifiRemote TCP 47801" dir=in action=allow protocol=TCP localport=47801 profile=any >nul 2>&1
netsh advfirewall firewall add rule name="WifiRemote UDP 47800" dir=in action=allow protocol=UDP localport=47800 profile=any >nul 2>&1
netsh advfirewall firewall add rule name="WifiRemote TLS 47803" dir=in action=allow protocol=TCP localport=47803 profile=any >nul 2>&1

rem Optional packages: Pillow (lighter screen frames) and anthropic (Claude). Installed once if missing.
%PY% -c "import PIL, anthropic" >nul 2>&1 || (
  echo Installing optional packages: pillow, anthropic ...
  %PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt
)

echo Starting Wi-Fi Remote...
%PY% -u pc_server.py
pause

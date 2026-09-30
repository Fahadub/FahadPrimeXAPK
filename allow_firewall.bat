@echo off
REM Run this file as ADMINISTRATOR (right-click -> Run as administrator)
echo Adding Windows Firewall rules for Wi-Fi Remote...
netsh advfirewall firewall delete rule name="WifiRemote TCP 47801" >nul 2>&1
netsh advfirewall firewall delete rule name="WifiRemote UDP 47800" >nul 2>&1
netsh advfirewall firewall delete rule name="WifiRemote TLS 47803" >nul 2>&1
netsh advfirewall firewall add rule name="WifiRemote TCP 47801" dir=in action=allow protocol=TCP localport=47801 profile=any
netsh advfirewall firewall add rule name="WifiRemote UDP 47800" dir=in action=allow protocol=UDP localport=47800 profile=any
netsh advfirewall firewall add rule name="WifiRemote TLS 47803" dir=in action=allow protocol=TCP localport=47803 profile=any
echo.
echo Done. You can now pair the phone.
pause

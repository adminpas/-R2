@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Al-Najeeb Ecosystem - Local Pilot
if not defined NAJEEB_BACKUP_KEY_FILE set "NAJEEB_BACKUP_KEY_FILE=%USERPROFILE%\.najeeb-ecosystem\backup.key"
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
if not exist storage mkdir storage
if not exist data mkdir data
echo.
echo ============================================================
echo  Al-Najeeb Ecosystem - Local Test Version
echo ============================================================
echo  Customer showroom: http://127.0.0.1:8765/
echo  Separate admin app: http://127.0.0.1:8765/admin/
echo  First login: admin / NajeebDemo#2026
echo  You will be required to change the username/password.
echo.
echo  To use external drive E:, set ALNAJEEB_DATA_ROOT=E:\Al_Najeeb_SSD
echo  Payment gateway, OTP, biometrics and cloud AI are not enabled.
echo  Do not use real customer or payment data.
echo ============================================================
%PY% server.py
pause

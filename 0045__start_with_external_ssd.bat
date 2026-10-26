@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "ALNAJEEB_DATA_ROOT=E:\Al_Najeeb_SSD"
set "PORT=8765"
if not defined NAJEEB_BACKUP_KEY_FILE set "NAJEEB_BACKUP_KEY_FILE=%USERPROFILE%\.najeeb-ecosystem\backup.key"
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
echo Starting with external storage: %ALNAJEEB_DATA_ROOT%
if not exist E:\Al_Najeeb_SSD (echo ERROR: E:\Al_Najeeb_SSD was not found. Connect/prepare the drive first. & pause & exit /b 1)
%PY% server.py
pause

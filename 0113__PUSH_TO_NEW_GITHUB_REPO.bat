@echo off
setlocal
cd /d "%~dp0"
where git >nul 2>nul
if errorlevel 1 (echo ERROR: Git is not installed. Install Git for Windows first. & pause & exit /b 1)
if exist ".git" (echo ERROR: This directory already has .git. Stop to avoid overwriting an existing repository. & pause & exit /b 2)
python scripts\validate_repo.py
if errorlevel 1 (echo ERROR: Repository validation failed. & pause & exit /b 3)
echo.
echo Create a NEW EMPTY PRIVATE repository on GitHub first. Do not upload individual files in the browser.
set /p REMOTE_URL=Paste the HTTPS URL of the NEW EMPTY repository: 
if "%REMOTE_URL%"=="" (echo ERROR: URL is required. & pause & exit /b 4)
git init -b main
if errorlevel 1 goto fail
git add .
git status --short
echo.
echo Review the file list above. If it contains secrets or unexpected files, press Ctrl+C now.
pause
git commit -m "Initial clean Najib reference repository"
if errorlevel 1 goto fail
git remote add origin "%REMOTE_URL%"
if errorlevel 1 goto fail
git push -u origin main
if errorlevel 1 goto fail
echo SUCCESS: Clean repository pushed. Connect this repository in Arena Agent Mode.
pause
exit /b 0
:fail
echo ERROR: Git operation failed. No files were intentionally deleted. Check the message above.
pause
exit /b 5

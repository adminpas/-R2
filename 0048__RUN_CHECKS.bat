@echo off
setlocal
cd /d "%~dp0"
python scripts\validate_repo.py || goto fail
python -m py_compile app\server.py app\backup_engine.py app\cloud_delivery.py app\tools\setup_backup_key.py || goto fail
node --check app\admin\assets\admin.js || goto fail
node --check app\merchant\assets\merchant.js || goto fail
python app\tools\smoke_test.py || goto fail
echo ALL CHECKS PASSED
pause
exit /b 0
:fail
echo CHECKS FAILED - read output above
pause
exit /b 1

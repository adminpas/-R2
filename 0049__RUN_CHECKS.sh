#!/usr/bin/env sh
set -eu
python scripts/validate_repo.py
python -m py_compile app/server.py app/backup_engine.py app/cloud_delivery.py app/tools/setup_backup_key.py
node --check app/admin/assets/admin.js
node --check app/merchant/assets/merchant.js
python app/tools/smoke_test.py

#!/usr/bin/env python3
"""Fast repository hygiene checks. Exit nonzero on errors."""
from pathlib import Path
import hashlib, sys
ROOT = Path(__file__).resolve().parents[1]
errors=[]; warnings=[]; seen={}
for p in ROOT.rglob('*'):
    if not p.is_file(): continue
    rel=p.relative_to(ROOT).as_posix()
    if any(x in rel.split('/') for x in ('.git','__pycache__','.venv','node_modules')): continue
    low=rel.lower()
    if low.endswith(('.zip','.7z','.rar','.sqlite','.sqlite3','.db','.pyc','.njb')) or low.endswith(('-wal','-shm')):
        errors.append(f'Forbidden generated/duplicate bundle/runtime artifact: {rel}')
    if p.name=='.env': errors.append(f'Secret file must never be committed: {rel}')
    if p.stat().st_size > 25*1024*1024: warnings.append(f'Large file (>25 MiB): {rel} ({p.stat().st_size})')
    digest=hashlib.sha256(p.read_bytes()).hexdigest()
    # Ignore duplicates for purely generated docs? detect duplicate exact binary/code regardless, except package metadata.
    if digest in seen and p.suffix.lower() in ('.py','.js','.html','.css','.json','.md','.csv','.sql','.mmd','.svg'):
        warnings.append(f'Exact duplicate content: {seen[digest]} == {rel}')
    else: seen[digest]=rel
for required in ('AGENTS.md','ARENA_TASK.md','FILE_TASK_QUEUE.csv','app/server.py','app/tools/smoke_test.py','docs/01_MASTER_PROJECT_REFERENCE_AR.md'):
    if not (ROOT/required).exists(): errors.append(f'Missing required path: {required}')
for x in warnings: print('WARN:',x)
for x in errors: print('ERROR:',x)
if errors:
    print(f'FAIL: {len(errors)} error(s), {len(warnings)} warning(s)'); sys.exit(1)
print(f'PASS: repo hygiene; {sum(1 for p in ROOT.rglob("*") if p.is_file())} files; {len(warnings)} warning(s).')

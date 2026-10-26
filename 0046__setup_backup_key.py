#!/usr/bin/env python3
"""Create a dedicated backup encryption key file; the key is never printed."""
import argparse, base64, os, secrets, subprocess, sys
from pathlib import Path

def main():
    ap=argparse.ArgumentParser(description='Create external local key file for Najeeb encrypted backups')
    ap.add_argument('--path',default=str(Path.home()/'.najeeb-ecosystem'/'backup.key'))
    ap.add_argument('--rotate',action='store_true',help='Replace an existing key (this makes old backups unreadable unless old key is saved)')
    args=ap.parse_args(); p=Path(args.path).expanduser().resolve(); p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists() and not args.rotate:
        print(f'Key file already exists; left unchanged: {p}')
        print('Set NAJEEB_BACKUP_KEY_FILE to this path. Keep a separate offline copy of the key.')
        return 0
    raw=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii')+'\n'
    tmp=p.with_suffix(p.suffix+'.new'); tmp.write_text(raw,encoding='ascii'); os.chmod(tmp,0o600); os.replace(tmp,p); os.chmod(p,0o600)
    if os.name=='nt':
        try:
            who=subprocess.check_output(['whoami'],text=True,timeout=5).strip()
            subprocess.run(['icacls',str(p),'/inheritance:r','/grant:r',f'{who}:(R,W)'],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        except Exception: pass
    print(f'Created key file: {p}')
    print('The secret was not printed. Set NAJEEB_BACKUP_KEY_FILE to this path.')
    print('CRITICAL: copy this key securely to offline/independent secret storage. Losing it makes encrypted backups unreadable; rotating it requires preserving the old key for old backups.')
    return 0
if __name__=='__main__': raise SystemExit(main())

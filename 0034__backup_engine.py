#!/usr/bin/env python3
"""Najeeb encrypted backup + readable report exporter (pilot release).

Encrypted backup format: NJBK1 || 12-byte AES-GCM nonce || ciphertext+tag.
The encryption key is supplied separately through NAJEEB_BACKUP_KEY and must
never be stored beside backup files. Requires `cryptography` package.
"""
from __future__ import annotations
import argparse, base64, csv, datetime as dt, hashlib, io, json, os, secrets, shutil, sqlite3, stat, sys, tarfile, tempfile, time
from pathlib import Path

MAGIC = b'NJBK1'
TABLES_PREFERRED = [
    'users','categories','products','reservations','quote_requests','ad_slots','audit_log',
    # Export if present as the application evolves; the full SQLite snapshot is always included.
    'merchants','spare_part_categories','spare_parts','orders','order_items','invoices','payments',
    'refunds','ledger_accounts','ledger_entries','ledger_lines','stock_movements','maintenance_tickets',
    'security_events','media_assets','backup_events'
]
SECRET_COLUMN_PARTS = ('password','secret','token','api_key','private_key','credential','session')


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

def iso(dt_obj=None):
    return (dt_obj or utc_now()).isoformat().replace('+00:00','Z')

def key_bytes_from_env():
    raw = os.environ.get('NAJEEB_BACKUP_KEY', '').strip()
    if not raw:
        key_file = os.environ.get('NAJEEB_BACKUP_KEY_FILE', '').strip()
        if key_file:
            try: raw = Path(key_file).expanduser().read_text(encoding='ascii').strip()
            except OSError as e: raise RuntimeError(f'تعذر قراءة ملف مفتاح النسخ الاحتياطي المحدد: {e}') from e
    if not raw:
        raise RuntimeError('مفتاح التشفير غير مضبوط؛ اضبط NAJEEB_BACKUP_KEY أو NAJEEB_BACKUP_KEY_FILE.')
    try:
        key = base64.urlsafe_b64decode(raw.encode('ascii'))
    except Exception as e:
        raise RuntimeError('صيغة مفتاح النسخ الاحتياطي غير صحيحة؛ استخدم المفتاح الذي يولده backup_engine.py.') from e
    if len(key) != 32:
        raise RuntimeError('طول مفتاح النسخ الاحتياطي غير صحيح؛ يجب أن يمثل 32 بايت بعد فك Base64.')
    return key

def crypto_primitives():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        return Cipher, algorithms, modes
    except ImportError as e:
        raise RuntimeError('يلزم تثبيت مكتبة cryptography قبل استخدام التشفير: python -m pip install -r requirements-backup.txt') from e

def aesgcm_class():
    # Compatibility/status helper; actual bundle encryption uses streaming AES-256-GCM.
    crypto_primitives()
    return True

def sha256_file(path: Path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def safe_chmod(path: Path, mode: int):
    try: path.chmod(mode)
    except OSError: pass

def readable_csvs(db_snapshot: Path, target: Path):
    target.mkdir(parents=True, exist_ok=True)
    safe_chmod(target, 0o700)
    result=[]
    con=sqlite3.connect(str(db_snapshot)); con.row_factory=sqlite3.Row
    try:
        present={r['name'] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        selected=[t for t in TABLES_PREFERRED if t in present]
        # Also export application tables not listed above; never export columns that look like secrets.
        selected += sorted(present-set(selected))
        for table in selected:
            cols=[r['name'] for r in con.execute(f'PRAGMA table_info("{table.replace(chr(34), chr(34)*2)}")')]
            cols=[c for c in cols if not any(part in c.lower() for part in SECRET_COLUMN_PARTS)]
            if not cols: continue
            quoted=', '.join('"'+c.replace('"','""')+'"' for c in cols)
            rows=con.execute(f'SELECT {quoted} FROM "{table.replace(chr(34), chr(34)*2)}"')
            out=target/(table+'.csv')
            with out.open('w',encoding='utf-8-sig',newline='') as f:
                writer=csv.writer(f); writer.writerow(cols)
                count=0
                for row in rows:
                    writer.writerow([json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else ('' if v is None else v) for v in row]); count+=1
            safe_chmod(out,0o600)
            result.append({'table':table,'file':out.name,'rows':count,'sha256':sha256_file(out),'columns':cols})
    finally: con.close()
    # Human-readable inventory, explicitly not a substitute for the complete encrypted database.
    info={'created_at':iso(),'purpose':'تقارير مقروءة للمراجعة؛ لا تستبدل نسخة قاعدة البيانات المشفرة.','tables':result}
    (target/'REPORT_MANIFEST.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    safe_chmod(target/'REPORT_MANIFEST.json',0o600)
    return info

def db_snapshot(source_db: Path, dest_db: Path):
    if not source_db.is_file(): raise RuntimeError(f'قاعدة البيانات غير موجودة: {source_db}')
    src=sqlite3.connect(str(source_db), timeout=30)
    dst=sqlite3.connect(str(dest_db), timeout=30)
    try:
        src.backup(dst, pages=256, sleep=0.05)
        check=dst.execute('PRAGMA integrity_check').fetchone()[0]
        if check!='ok': raise RuntimeError(f'فشل فحص سلامة النسخة: {check}')
    finally:
        dst.close(); src.close()

def pack_dir(folder: Path, out_path: Path):
    with tarfile.open(out_path,'w:gz',compresslevel=6) as tf:
        for p in sorted(folder.rglob('*')):
            if p.resolve() == out_path.resolve():
                continue
            if p.is_file() and not p.is_symlink():
                tf.add(p,arcname=p.relative_to(folder).as_posix(),recursive=False)

def create_backup(db_path: Path, media_root: Path, backup_root: Path, *, retain_days=45):
    AESGCM=aesgcm_class(); key=key_bytes_from_env()
    db_path=Path(db_path).resolve(); media_root=Path(media_root).resolve(); backup_root=Path(backup_root).expanduser().resolve()
    backup_root.mkdir(parents=True,exist_ok=True); safe_chmod(backup_root,0o700)
    stamp=utc_now().strftime('%Y%m%dT%H%M%S_%fZ')
    reports_root=backup_root/'readable_reports'/utc_now().strftime('%Y-%m-%d')
    reports_root.mkdir(parents=True,exist_ok=True); safe_chmod(reports_root,0o700)
    with tempfile.TemporaryDirectory(prefix='najeeb_backup_') as td:
        stage=Path(td); snapshot=stage/'najeeb.sqlite3'
        db_snapshot(db_path,snapshot)
        report_stage=stage/'reports'; report_info=readable_csvs(snapshot,report_stage)
        with sqlite3.connect(str(snapshot)) as con:
            table_count=con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
        files=[]
        for p in [snapshot,*sorted(report_stage.rglob('*'))]:
            if p.is_file(): files.append({'path':p.relative_to(stage).as_posix(),'size':p.stat().st_size,'sha256':sha256_file(p)})
        manifest={
            'format':'NajeebEncryptedBackup','format_version':1,'created_at':iso(),
            'database_name':db_path.name,'database_sha256':sha256_file(snapshot),
            'database_integrity':'ok','database_table_count':table_count,
            'media_included':False,
            'reason_media_not_included':'نسخة البيانات التشغيلية لا تضم الصور والفيديوهات لتقليل الحجم؛ تحفظ الوسائط بسياسة منفصلة.',
            'readable_reports':report_info,'files':files,
            'warning':'تحتاج الاستعادة إلى مفتاح التشفير المحفوظ خارج هذه الحزمة.'
        }
        (stage/'MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        tgz=stage/'payload.tar.gz'; pack_dir(stage,tgz)
        Cipher,algorithms,modes=crypto_primitives()
        nonce=secrets.token_bytes(12)
        out=backup_root/f'najeeb-backup-{stamp}.njb'
        partial=out.with_suffix(out.suffix+'.partial')
        encryptor=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(MAGIC)
        with tgz.open('rb') as inf, partial.open('xb') as outf:
            outf.write(MAGIC+nonce)
            for chunk in iter(lambda:inf.read(1024*1024),b''): outf.write(encryptor.update(chunk))
            outf.write(encryptor.finalize()); outf.write(encryptor.tag); outf.flush(); os.fsync(outf.fileno())
        os.replace(partial,out); safe_chmod(out,0o600)
        # Generate readable CSVs separately for local review only. They may contain personal/financial data.
        daily=reports_root/stamp
        if daily.exists(): shutil.rmtree(daily)
        shutil.copytree(report_stage,daily)
        summary={'ok':True,'file':out.name,'path':str(out),'size_bytes':out.stat().st_size,'sha256':sha256_file(out),
                 'created_at':iso(),'database_integrity':'ok','tables':table_count,'readable_report_dir':str(daily),
                 'media_included':False,'encrypted':True}
    cleanup_old_backups(backup_root,retain_days)
    return summary

def cleanup_old_backups(root: Path, retain_days: int):
    if retain_days < 7: retain_days=7
    cutoff=time.time()-retain_days*86400
    # Only delete this application's own encrypted bundles; readable reports are retained under a separate policy.
    for p in root.glob('najeeb-backup-*.njb'):
        try:
            if p.is_file() and not p.is_symlink() and p.stat().st_mtime < cutoff: p.unlink()
        except OSError: pass

def backup_status(root: Path):
    root=Path(root).expanduser().resolve(); files=[]
    if root.exists():
        for p in sorted(root.glob('najeeb-backup-*.njb'),key=lambda x:x.stat().st_mtime,reverse=True)[:25]:
            try: files.append({'file':p.name,'created_at':dt.datetime.fromtimestamp(p.stat().st_mtime,dt.timezone.utc).isoformat().replace('+00:00','Z'),'size_bytes':p.stat().st_size,'sha256':sha256_file(p)})
            except OSError: pass
    try: key_ok=len(key_bytes_from_env())==32
    except Exception: key_ok=False
    try: crypto_ok=bool(aesgcm_class())
    except Exception: crypto_ok=False
    return {'configured':key_ok and crypto_ok,'key_configured':key_ok,'crypto_available':crypto_ok,'root':str(root),'count':len(files),'latest':files[0] if files else None,'files':files,
            'schedule':'يوميًا عند الساعة المحددة في NAJEEB_BACKUP_HOUR إذا كانت خدمة الجدولة مفعلة؛ وإلا يمكن الإنشاء يدويًا.'}

def decrypt_bundle(bundle: Path, output: Path, key: bytes, *, apply_db: Path|None=None):
    Cipher,algorithms,modes=crypto_primitives()
    bundle=Path(bundle).resolve(); size=bundle.stat().st_size
    if size<5+12+16: raise RuntimeError('الملف ليس حزمة نجيب معتمدة أو أنه تالف.')
    with bundle.open('rb') as bf:
        header=bf.read(17)
        if header[:5]!=MAGIC: raise RuntimeError('الملف ليس حزمة نجيب معتمدة أو أنه تالف.')
        nonce=header[5:17]; tag=header[-16:]
        bf.seek(size-16); tag=bf.read(16); bf.seek(17)
        cipher_bytes=size-17-16
        output=Path(output).expanduser().resolve(); output.mkdir(parents=True,exist_ok=True); safe_chmod(output,0o700)
        with tempfile.TemporaryDirectory(prefix='najeeb_restore_') as td:
            tgz=Path(td)/'payload.tar.gz'
            try:
                decryptor=Cipher(algorithms.AES(key),modes.GCM(nonce,tag)).decryptor(); decryptor.authenticate_additional_data(MAGIC)
                with tgz.open('wb') as outf:
                    remaining=cipher_bytes
                    while remaining>0:
                        chunk=bf.read(min(1024*1024,remaining))
                        if not chunk: raise RuntimeError('انتهى ملف النسخة قبل اكتمال المحتوى.')
                        outf.write(decryptor.update(chunk)); remaining-=len(chunk)
                    outf.write(decryptor.finalize())
            except Exception as e: raise RuntimeError('فشل التحقق من التوقيع/التشفير؛ المفتاح خطأ أو النسخة تالفة.') from e
            # Extract regular files/directories only, with explicit traversal and symlink rejection.
            with tarfile.open(tgz,'r:gz') as tf:
                members=tf.getmembers()
                for m in members:
                    rel=Path(m.name)
                    if rel.is_absolute() or '..' in rel.parts or not (m.isfile() or m.isdir()): raise RuntimeError('الحزمة تحتوي مسارًا غير آمن أو نوع ملف غير مسموح.')
                for m in members:
                    target=(output/Path(m.name)).resolve()
                    if output!=target and output not in target.parents: raise RuntimeError('مسار استخراج يتجاوز مجلد الاستعادة.')
                    if m.isdir(): target.mkdir(parents=True,exist_ok=True); continue
                    target.parent.mkdir(parents=True,exist_ok=True)
                    source=tf.extractfile(m)
                    if source is None: raise RuntimeError(f'تعذر قراءة الملف: {m.name}')
                    with source, target.open('wb') as dest: shutil.copyfileobj(source,dest,1024*1024)
                    safe_chmod(target,0o600)
    manifest_path=output/'MANIFEST.json'; db_file=output/'najeeb.sqlite3'
    if not manifest_path.is_file() or not db_file.is_file(): raise RuntimeError('ملفات الاستعادة الأساسية غير مكتملة.')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if sha256_file(db_file)!=manifest.get('database_sha256'): raise RuntimeError('فشل التحقق من بصمة قاعدة البيانات بعد فك التشفير.')
    for item in manifest.get('files', []):
        rel=Path(item.get('path',''))
        candidate=(output/rel).resolve()
        if rel.is_absolute() or '..' in rel.parts or output not in candidate.parents or not candidate.is_file():
            raise RuntimeError(f'ملف مفقود أو مسار غير آمن في بيان النسخة: {rel}')
        if sha256_file(candidate)!=item.get('sha256'): raise RuntimeError(f'فشل التحقق من بصمة الملف: {rel}')
    with sqlite3.connect(str(db_file)) as c:
        check=c.execute('PRAGMA integrity_check').fetchone()[0]
    if check!='ok': raise RuntimeError(f'فشل فحص قاعدة البيانات المستعادة: {check}')
    if apply_db:
        target=Path(apply_db).expanduser().resolve()
        if target==db_file or target.is_dir(): raise RuntimeError('مسار استعادة قاعدة البيانات غير صالح.')
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            safety=target.with_name(target.name+f'.pre-restore-{utc_now().strftime("%Y%m%dT%H%M%SZ")}')
            shutil.copy2(target,safety); safe_chmod(safety,0o600)
        tmp=target.with_name(target.name+'.restore-tmp')
        shutil.copy2(db_file,tmp); safe_chmod(tmp,0o600); os.replace(tmp,target)
    return {'ok':True,'output_dir':str(output),'database':str(db_file),'database_integrity':check,'created_at':manifest.get('created_at'),'table_count':manifest.get('database_table_count'),'applied_to':str(apply_db) if apply_db else None,'note':'لم يتم استرجاع ملفات الصور والفيديوهات؛ النسخة تخص قاعدة البيانات والنسخ المقروءة.'}

def main():
    ap=argparse.ArgumentParser(description='Najeeb backup and restore utility')
    sub=ap.add_subparsers(dest='cmd',required=True)
    sub.add_parser('generate-key',help='Generate a 32-byte AES-256 key; save it offline, not beside backups')
    c=sub.add_parser('backup',help='Create an encrypted SQLite backup and readable CSV reports')
    c.add_argument('--db',default=os.environ.get('NAJEEB_DB_PATH','data/najeeb-pilot.sqlite3'))
    c.add_argument('--media-root',default=os.environ.get('ALNAJEEB_DATA_ROOT','storage'))
    c.add_argument('--output',default=os.environ.get('NAJEEB_BACKUP_ROOT','system_backups'))
    c.add_argument('--retain-days',type=int,default=int(os.environ.get('NAJEEB_BACKUP_RETAIN_DAYS','90')))
    v=sub.add_parser('restore',help='Decrypt and validate a backup into a separate recovery folder by default')
    v.add_argument('bundle'); v.add_argument('--output',default='recovery_output'); v.add_argument('--apply-db',default='',help='Optional target DB path; stop the app first. A pre-restore copy is retained.')
    v.add_argument('--key-env',default='NAJEEB_BACKUP_KEY'); v.add_argument('--key-file',default=os.environ.get('NAJEEB_BACKUP_KEY_FILE',''),help='Path to an external key file; preferred over environment key when supplied')
    args=ap.parse_args()
    try:
        if args.cmd=='generate-key':
            key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii')
            print('احفظ المفتاح التالي في مدير أسرار أو وسيط غير متصل منفصل عن النسخ. لن يُحفظ المفتاح في ملف تلقائيًا:')
            print(key); return 0
        if args.cmd=='backup':
            result=create_backup(Path(args.db),Path(args.media_root),Path(args.output),retain_days=args.retain_days)
            print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
        if args.cmd=='restore':
            raw=os.environ.get(args.key_env,'').strip()
            if args.key_file:
                try: raw=Path(args.key_file).expanduser().read_text(encoding='ascii').strip()
                except OSError as e: raise RuntimeError(f'تعذر قراءة ملف المفتاح: {e}') from e
            if not raw and os.environ.get('NAJEEB_BACKUP_KEY_FILE','').strip(): raw=Path(os.environ['NAJEEB_BACKUP_KEY_FILE']).expanduser().read_text(encoding='ascii').strip()
            if not raw: raise RuntimeError(f'المتغير {args.key_env} أو ملف المفتاح غير مضبوط.')
            key=base64.urlsafe_b64decode(raw.encode('ascii'))
            if len(key)!=32: raise RuntimeError('مفتاح التشفير غير صالح.')
            result=decrypt_bundle(Path(args.bundle),Path(args.output),key,apply_db=Path(args.apply_db) if args.apply_db else None)
            print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
    except Exception as e:
        print(f'ERROR: {e}',file=sys.stderr); return 2
    return 1
if __name__=='__main__': raise SystemExit(main())

#!/usr/bin/env python3
"""Al-Najeeb Ecosystem local pilot: standard-library-only HTTP server + SQLite."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from pathlib import Path
from email.parser import BytesParser
from email.policy import default
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import os, json, sqlite3, hashlib, hmac, secrets, mimetypes, urllib.parse, re, sys, threading, time, shutil, subprocess
from backup_engine import create_backup, backup_status
from cloud_delivery import upload_s3, notify_backup, s3_is_configured, telegram_is_configured, email_is_configured

BASE = Path(__file__).resolve().parent
STATIC = BASE / 'static'
ADMIN = BASE / 'admin'
MERCHANT = BASE / 'merchant'
DATA = Path(os.environ.get('ALNAJEEB_DATA_ROOT', str(BASE / 'storage'))).expanduser().resolve()
DB = Path(os.environ.get('NAJEEB_DB_PATH', str(BASE / 'data' / 'najeeb-pilot.sqlite3'))).expanduser().resolve()
DBDIR = DB.parent
MAX_UPLOAD = 250 * 1024 * 1024
PORT = int(os.environ.get('PORT', '8765'))
HOST = os.environ.get('HOST', '127.0.0.1')
BACKUP_ROOT = Path(os.environ.get('NAJEEB_BACKUP_ROOT', str(BASE / 'system_backups'))).expanduser().resolve()
BACKUP_LOCK = threading.Lock()
SESSIONS = {}
SESSION_TTL = 8 * 60 * 60

def now_utc(): return datetime.now(timezone.utc).replace(microsecond=0)
def stamp(dt=None): return (dt or now_utc()).isoformat().replace('+00:00','Z')
def parse_stamp(value):
    try: return datetime.fromisoformat(value.replace('Z','+00:00'))
    except Exception: return now_utc()
class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()

def dbconn():
    DBDIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=20, isolation_level=None, factory=ClosingConnection)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=20000')
    return con

def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 310000)
    return salt.hex() + '$' + digest.hex()
def password_verify(password, saved):
    try:
        salt_hex, digest_hex = saved.split('$', 1)
        got = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), 310000).hex()
        return hmac.compare_digest(got, digest_hex)
    except Exception: return False

def safe_filename(name):
    name = Path(name.replace('\\','/')).name
    name = re.sub(r'[^\w.\-() ]+', '_', name, flags=re.UNICODE).strip(' .')[:160]
    return name or 'file.bin'

def init_storage():
    root_dirs = [
        '00_Quarantine','01_Categories_Media/Washers/Computer_Washers','01_Categories_Media/Washers/Hotel_Washers','01_Categories_Media/Washers/Heavy_Duty_Sub',
        '01_Categories_Media/Dryers/Gas_Dryers','01_Categories_Media/Dryers/Electric_Dryers','01_Categories_Media/Carpet_Lines','01_Categories_Media/Presses',
        '02_Equipment_Videos','03_Products_Images','04_Spare_Parts/Merchant_1','04_Spare_Parts/Merchant_2','04_Spare_Parts/Merchant_3',
        '05_System_Backups','06_Security_Blackbox','07_3D_Models','08_Service_Documents'
    ]
    for d in root_dirs: (DATA / d).mkdir(parents=True, exist_ok=True)

def init_db():
    init_storage()
    with dbconn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('SUPER_ADMIN','ASSISTANT_ADMIN','MERCHANT','MAINTENANCE_TECH','SHIPPING_COMPANY')), is_active INTEGER NOT NULL DEFAULT 1, must_change_password INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT, name_ar TEXT NOT NULL, name_en TEXT NOT NULL DEFAULT '', folder_path TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, category_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT, merchant_id INTEGER REFERENCES users(id), title_ar TEXT NOT NULL, title_en TEXT NOT NULL DEFAULT '', brand TEXT NOT NULL DEFAULT '', price REAL NOT NULL DEFAULT 0 CHECK(price>=0), condition TEXT NOT NULL DEFAULT 'USED_CLEAN' CHECK(condition IN ('NEW_4K','NEAR_NEW_BLURAY','USED_CLEAN','REFURBISHED','READY_NO_MAINTENANCE')), availability TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK(availability IN ('AVAILABLE','HOLD_24H','SOLD')), description TEXT NOT NULL DEFAULT '', specs_json TEXT NOT NULL DEFAULT '{}', image_path TEXT NOT NULL DEFAULT '', video_path TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id), customer_name TEXT NOT NULL, phone TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','EXPIRED','CANCELLED','CONVERTED')), created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_hold_per_product ON reservations(product_id) WHERE status='ACTIVE';
        CREATE TABLE IF NOT EXISTS quote_requests(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id), customer_name TEXT NOT NULL, phone TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'NEW');
        CREATE TABLE IF NOT EXISTS ad_slots(id INTEGER PRIMARY KEY, slot_key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, subtitle TEXT NOT NULL DEFAULT '', target_url TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, user_id INTEGER, event TEXT NOT NULL, object_type TEXT NOT NULL DEFAULT '', object_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS merchant_stores(
          id INTEGER PRIMARY KEY, store_key TEXT UNIQUE NOT NULL, slug TEXT UNIQUE NOT NULL,
          display_name TEXT NOT NULL, owner_user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE SET NULL,
          status TEXT NOT NULL DEFAULT 'PENDING_REVIEW' CHECK(status IN ('PENDING_REVIEW','APPROVED','SUSPENDED')),
          contact_name TEXT NOT NULL DEFAULT '', contact_phone TEXT NOT NULL DEFAULT '', contact_email TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS spare_part_categories(
          id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES spare_part_categories(id) ON DELETE RESTRICT,
          name_ar TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS spare_parts(
          id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL REFERENCES merchant_stores(id) ON DELETE RESTRICT,
          category_id INTEGER NOT NULL REFERENCES spare_part_categories(id) ON DELETE RESTRICT,
          name_ar TEXT NOT NULL, name_en TEXT NOT NULL DEFAULT '', part_number TEXT NOT NULL DEFAULT '',
          brand TEXT NOT NULL DEFAULT '', compatible_equipment TEXT NOT NULL DEFAULT '', model_range TEXT NOT NULL DEFAULT '',
          specifications TEXT NOT NULL DEFAULT '', application TEXT NOT NULL DEFAULT '', condition TEXT NOT NULL DEFAULT 'NEW',
          unit_price REAL NOT NULL DEFAULT 0 CHECK(unit_price>=0), currency TEXT NOT NULL DEFAULT 'SAR',
          stock_quantity INTEGER NOT NULL DEFAULT 0 CHECK(stock_quantity>=0), description TEXT NOT NULL DEFAULT '',
          image_path TEXT NOT NULL DEFAULT '', video_path TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'PENDING_REVIEW' CHECK(status IN ('DRAFT','PENDING_REVIEW','PUBLISHED','REJECTED','ARCHIVED')),
          review_note TEXT NOT NULL DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS spare_parts_store_status_idx ON spare_parts(store_id,status,updated_at);
        CREATE INDEX IF NOT EXISTS spare_parts_part_number_idx ON spare_parts(part_number);
        CREATE TABLE IF NOT EXISTS security_quarantine(
          id INTEGER PRIMARY KEY, target_type TEXT NOT NULL CHECK(target_type IN ('PRODUCT','SPARE_PART')),
          target_id INTEGER NOT NULL, store_id INTEGER REFERENCES merchant_stores(id),
          uploader_user_id INTEGER REFERENCES users(id), media_kind TEXT NOT NULL CHECK(media_kind IN ('image','video')),
          original_name TEXT NOT NULL, stored_path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
          sha256 TEXT NOT NULL, declared_content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
          scan_status TEXT NOT NULL DEFAULT 'QUARANTINED' CHECK(scan_status IN ('QUARANTINED','CLEAN','INFECTED','SCAN_UNAVAILABLE','RELEASED','REJECTED')),
          scan_engine TEXT NOT NULL DEFAULT '', scan_detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, scanned_at TEXT NOT NULL DEFAULT '', released_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS security_quarantine_status_idx ON security_quarantine(scan_status,created_at);
        CREATE TABLE IF NOT EXISTS financial_accounts(
          id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name_ar TEXT NOT NULL,
          account_type TEXT NOT NULL CHECK(account_type IN ('ASSET','LIABILITY','EQUITY','REVENUE','EXPENSE')),
          store_id INTEGER REFERENCES merchant_stores(id) ON DELETE RESTRICT,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ledger_transactions(
          id INTEGER PRIMARY KEY, txn_key TEXT UNIQUE NOT NULL, txn_type TEXT NOT NULL,
          reference_type TEXT NOT NULL DEFAULT '', reference_id TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'POSTED' CHECK(status IN ('POSTED','REVERSED')),
          created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ledger_entries(
          id INTEGER PRIMARY KEY, transaction_id INTEGER NOT NULL REFERENCES ledger_transactions(id) ON DELETE RESTRICT,
          account_id INTEGER NOT NULL REFERENCES financial_accounts(id) ON DELETE RESTRICT,
          side TEXT NOT NULL CHECK(side IN ('DEBIT','CREDIT')), amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
          currency TEXT NOT NULL CHECK(currency IN ('SAR','YER','USD')), memo TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ledger_entries_txn_idx ON ledger_entries(transaction_id,id);
        CREATE INDEX IF NOT EXISTS ledger_entries_account_idx ON ledger_entries(account_id,currency,created_at);
        CREATE TRIGGER IF NOT EXISTS ledger_entries_no_update BEFORE UPDATE ON ledger_entries BEGIN SELECT RAISE(ABORT,'ledger entries are immutable; post a reversal'); END;
        CREATE TRIGGER IF NOT EXISTS ledger_entries_no_delete BEFORE DELETE ON ledger_entries BEGIN SELECT RAISE(ABORT,'ledger entries are immutable; post a reversal'); END;
        CREATE TRIGGER IF NOT EXISTS ledger_transactions_no_update BEFORE UPDATE ON ledger_transactions BEGIN SELECT RAISE(ABORT,'ledger transactions are immutable; post a reversal'); END;
        CREATE TRIGGER IF NOT EXISTS ledger_transactions_no_delete BEFORE DELETE ON ledger_transactions BEGIN SELECT RAISE(ABORT,'ledger transactions are immutable; post a reversal'); END;
        
        CREATE TABLE IF NOT EXISTS spare_part_quote_requests(
          id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES spare_parts(id) ON DELETE RESTRICT,
          store_id INTEGER NOT NULL REFERENCES merchant_stores(id) ON DELETE RESTRICT,
          customer_name TEXT NOT NULL, phone TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'NEW' CHECK(status IN ('NEW','CONTACTED','QUOTED','CLOSED')),
          created_at TEXT NOT NULL
        );
        ''')
        if c.execute('SELECT COUNT(*) n FROM categories').fetchone()['n'] == 0:
            categories = [
                (None,'غسالات صناعية','Industrial Washers','01_Categories_Media/Washers'),
                (1,'غسالات كمبيوتر','Computer-Controlled Washers','01_Categories_Media/Washers/Computer_Washers'),
                (1,'غسالات الفنادق','Hotel Washers','01_Categories_Media/Washers/Hotel_Washers'),
                (1,'غسالات ثقيلة','Heavy-Duty Washers','01_Categories_Media/Washers/Heavy_Duty_Sub'),
                (None,'نشّافات حرارية','Industrial Dryers','01_Categories_Media/Dryers'),
                (5,'نشّافات الغاز','Gas Dryers','01_Categories_Media/Dryers/Gas_Dryers'),
                (5,'نشّافات كهربائية','Electric Dryers','01_Categories_Media/Dryers/Electric_Dryers'),
                (None,'خطوط غسيل السجاد','Carpet Lines','01_Categories_Media/Carpet_Lines'),
                (None,'الكي والتشطيب','Presses & Finishing','01_Categories_Media/Presses'),
                (None,'غلايات البخار','Steam Boilers','01_Categories_Media/Steam_Boilers'),
                (None,'قطع الغيار','Spare Parts','04_Spare_Parts'),
                (None,'الصيانة والتركيب','Maintenance & Installation','08_Service_Documents'),
            ]
            # Seed with explicit ids to keep parent links deterministic
            for i,(parent,ar,en,path) in enumerate(categories,1):
                c.execute('INSERT INTO categories(id,parent_id,name_ar,name_en,folder_path,created_at) VALUES(?,?,?,?,?,?)',(i,parent,ar,en,path,stamp()))
        if c.execute('SELECT COUNT(*) n FROM products').fetchone()['n'] == 0:
            products = [
              (1,'غسالة صناعية 50 كجم','50 kg Industrial Washer','Girbau',62000,'NEW_4K','غسالة صناعية عالية التحمل للاستخدام التجاري مع تحكم قابل للبرمجة.','سعة: 50 كجم|الفئة: صناعية|الحالة: جديد'),
              (2,'غسالة فندقية متطورة','Advanced Hotel Washer','Electrolux Professional',78000,'NEW_4K','حل صناعي للفنادق والمغاسل المركزية، والمواصفات النهائية تعتمد على الموديل.','الاستخدام: الفنادق|النوع: صناعي|الحالة: جديد'),
              (5,'مجفف صناعي بالغاز','Industrial Gas Dryer','Primus',38000,'NEAR_NEW_BLURAY','مجفف حراري للاستخدام التجاري؛ يرجى مطابقة مصدر الطاقة والموديل قبل الشراء.','مصدر الطاقة: غاز|الاستخدام: تجاري|الحالة: شبه جديد'),
              (8,'خط غسيل سجاد متكامل','Complete Carpet Washing Line','Cleanvac',120000,'USED_CLEAN','خط متكامل لغسيل السجاد، تخضع التفاصيل الفنية لفحص المعدة قبل البيع.','الاستخدام: السجاد|الحالة: مستخدم نظيف'),
              (9,'مكبس صناعي للتشطيب','Industrial Finishing Press','Pony',45000,'REFURBISHED','مكبس تشطيب للمغاسل التجارية؛ السعر المعروض للتجربة وغير ملزم.','الاستخدام: التشطيب|الحالة: مجدد'),
              (11,'مجموعة قطع غيار صناعية','Industrial Spare Parts Set','متعدد العلامات',1850,'USED_CLEAN','مثال توضيحي لمجموعة قطع غيار؛ لا يمثل مخزونًا مؤكدًا.','الفئة: قطع غيار|الحالة: حسب القطعة'),
            ]
            for cat,title,en,brand,price,cond,desc,specstr in products:
                specs = dict(s.split(': ',1) if ': ' in s else s.split(':',1) for s in specstr.split('|'))
                c.execute('INSERT INTO products(category_id,title_ar,title_en,brand,price,condition,description,specs_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', (cat,title,en,brand,price,cond,desc,json.dumps(specs,ensure_ascii=False),stamp(),stamp()))
        if c.execute('SELECT COUNT(*) n FROM spare_part_categories').fetchone()['n']==0:
            for sid,name,slug in [
                (1,'قطع ميكانيكية','mechanical-parts'),(2,'قطع كهربائية وإلكترونية','electrical-electronic'),
                (3,'المضخات والصمامات','pumps-valves'),(4,'البخار والتشطيب','steam-finishing'),
                (5,'السيور والمحامل ومواد الاستهلاك','belts-bearings-consumables'),(6,'التحكم وPLC والحساسات','control-plc-sensors')]:
                c.execute('INSERT INTO spare_part_categories(id,name_ar,slug,active,created_at) VALUES(?,?,?,?,?)',(sid,name,slug,1,stamp()))
        for key,slug,name in [
            ('merchant_1','parts-merchant-1','معرض التاجر الأول'),
            ('merchant_2','parts-merchant-2','معرض التاجر الثاني'),
            ('merchant_3','parts-merchant-3','معرض التاجر الثالث')]:
            c.execute("INSERT OR IGNORE INTO merchant_stores(store_key,slug,display_name,status,created_at,updated_at) VALUES(?,?,?,'PENDING_REVIEW',?,?)",(key,slug,name,stamp(),stamp()))
        if c.execute('SELECT COUNT(*) n FROM financial_accounts').fetchone()['n']==0:
            for code,name,atype in [
                ('1000','النقدية والصندوق','ASSET'),('1100','حسابات التحصيل والتسوية','ASSET'),('1200','ذمم العملاء','ASSET'),
                ('2000','ذمم الموردين','LIABILITY'),('2100','عربون وحجوزات العملاء','LIABILITY'),
                ('4000','إيرادات المبيعات','REVENUE'),('4100','إيرادات العمولات المعتمدة','REVENUE'),
                ('5000','مصروفات الشحن والتركيب','EXPENSE'),('5100','مصروفات الصيانة','EXPENSE')]:
                c.execute('INSERT INTO financial_accounts(code,name_ar,account_type,store_id,active,created_at) VALUES(?,?,?,NULL,1,?)',(code,name,atype,stamp()))
        for st in c.execute('SELECT id,display_name FROM merchant_stores ORDER BY id').fetchall():
            code=f"2200-M{st['id']}"
            c.execute('INSERT OR IGNORE INTO financial_accounts(code,name_ar,account_type,store_id,active,created_at) VALUES(?,?,?, ?,1,?)',(code,'مستحقات '+st['display_name'],'LIABILITY',st['id'],stamp()))
        for key,title,sub in [
          ('hero','وصل حديثًا إلى معرض النجيب','معدات مغاسل صناعية مختارة — بيانات المعاينة تجريبية'),
          ('merchant_1','معرض التاجر الأول','مساحة إعلانية تجريبية لقطع الغيار'),
          ('merchant_2','معرض التاجر الثاني','مساحة إعلانية تجريبية لقطع الغيار'),
          ('merchant_3','معرض التاجر الثالث','مساحة إعلانية تجريبية لقطع الغيار'),
        ]:
          c.execute('INSERT OR IGNORE INTO ad_slots(slot_key,title,subtitle,target_url,active,updated_at) VALUES(?,?,?,?,1,?)',(key,title,sub,'',stamp()))
        if not c.execute('SELECT id FROM users WHERE username=?',('admin',)).fetchone():
            c.execute('INSERT INTO users(username,password_hash,role,is_active,must_change_password,created_at) VALUES(?,?,?,?,?,?)',('admin',password_hash('NajeebDemo#2026'),'SUPER_ADMIN',1,1,stamp()))
        for row in c.execute('SELECT folder_path FROM categories').fetchall():
            rel=str(row['folder_path']).replace('\\','/').strip('/')
            if rel and all(part not in ('.','..') for part in Path(rel).parts):
                target=(DATA/rel).resolve()
                if DATA in target.parents: target.mkdir(parents=True,exist_ok=True)

def rowdict(row): return dict(row) if row is not None else None

def expire_holds(c):
    ts=stamp()
    c.execute("UPDATE reservations SET status='EXPIRED' WHERE status='ACTIVE' AND expires_at<=?",(ts,))
    c.execute("UPDATE products SET availability='AVAILABLE', updated_at=? WHERE availability='HOLD_24H' AND id NOT IN (SELECT product_id FROM reservations WHERE status='ACTIVE' AND expires_at>?)",(ts,ts))
    c.execute("UPDATE products SET availability='HOLD_24H' WHERE id IN (SELECT product_id FROM reservations WHERE status='ACTIVE' AND expires_at>?) AND availability='AVAILABLE'",(ts,))

def audit(c, user_id, event, kind='', oid='', detail=''):
    c.execute('INSERT INTO audit_log(user_id,event,object_type,object_id,detail,created_at) VALUES(?,?,?,?,?,?)',(user_id,event,kind,str(oid),detail[:2000],stamp()))

def media_signature_ok(kind, suffix, head):
    if kind == 'image':
        return (suffix == '.png' and head.startswith(b'\x89PNG\r\n\x1a\n')) or (suffix in ('.jpg','.jpeg') and head.startswith(b'\xff\xd8\xff')) or (suffix == '.webp' and head.startswith(b'RIFF') and head[8:12] == b'WEBP') or (suffix == '.gif' and head[:6] in (b'GIF87a',b'GIF89a'))
    return (suffix in ('.mp4','.m4v','.mov') and len(head) >= 8 and head[4:8] == b'ftyp') or (suffix == '.webm' and head.startswith(b'\x1a\x45\xdf\xa3'))

def scan_with_clamav(filepath: Path):
    scanner=shutil.which('clamscan')
    if not scanner:
        return {'status':'SCAN_UNAVAILABLE','engine':'','detail':'ClamAV clamscan is not installed/configured on this server; file remains quarantined.'}
    try:
        cp=subprocess.run([scanner,'--no-summary','--infected',str(filepath)],capture_output=True,text=True,timeout=120,check=False)
        out=(cp.stdout+'\n'+cp.stderr).strip()[-2000:]
        if cp.returncode==0: return {'status':'CLEAN','engine':'ClamAV/clamscan','detail':'ClamAV did not detect a threat; admin approval is still required. '+out}
        if cp.returncode==1: return {'status':'INFECTED','engine':'ClamAV/clamscan','detail':'ClamAV detected a threat; publication is blocked. '+out}
        return {'status':'SCAN_UNAVAILABLE','engine':'ClamAV/clamscan','detail':f'Could not confirm scan result (exit={cp.returncode}). '+out}
    except subprocess.TimeoutExpired:
        return {'status':'SCAN_UNAVAILABLE','engine':'ClamAV/clamscan','detail':'Scan timed out; file remains quarantined.'}
    except Exception as exc:
        return {'status':'SCAN_UNAVAILABLE','engine':'ClamAV/clamscan','detail':str(exc)[:1000]}

class Handler(BaseHTTPRequestHandler):
    server_version = 'AlNajeebPilot/0.2'
    def log_message(self, fmt, *args):
        if os.environ.get('NAJEEB_ACCESS_LOG','0') == '1': super().log_message(fmt,*args)
    def _send(self, code, data=b'', ctype='text/html; charset=utf-8', headers=None):
        if isinstance(data,str): data=data.encode('utf-8')
        self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(data))); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','same-origin'); self.send_header('X-Frame-Options','DENY'); self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=()')
        headers=headers or {}
        if 'Cache-Control' not in headers: self.send_header('Cache-Control','no-store')
        for k,v in headers.items(): self.send_header(k,v)
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass
    def _json(self, code, obj, headers=None): self._send(code,json.dumps(obj,ensure_ascii=False).encode('utf-8'),'application/json; charset=utf-8',headers)
    def _body_json(self):
        length=int(self.headers.get('Content-Length','0'))
        if length>2*1024*1024: raise ValueError('الطلب أكبر من الحد المسموح.')
        raw=self.rfile.read(length) if length else b'{}'
        return json.loads(raw.decode('utf-8'))
    def _quarantine_upload(self, target_type, target_id, store_id, user_id):
        kind=self.headers.get('X-Media-Kind','image').lower()
        if kind not in ('image','video'): return self._json(400,{'error':'نوع الملف غير مدعوم.'})
        length=int(self.headers.get('Content-Length','0'))
        limit=25*1024*1024 if kind=='image' else 250*1024*1024
        if length<1 or length>limit: return self._json(413,{'error':'حجم الملف غير صالح أو يتجاوز الحد المخصص لهذا النوع.'})
        name=safe_filename(unquote(self.headers.get('X-File-Name','upload.bin')))
        suffix=Path(name).suffix.lower(); ctype=self.headers.get('Content-Type','application/octet-stream')
        allowed_image={'.png','.jpg','.jpeg','.webp','.gif'}; allowed_video={'.mp4','.m4v','.mov','.webm'}
        if suffix not in (allowed_image if kind=='image' else allowed_video):
            remaining=length
            while remaining>0:
                chunk=self.rfile.read(min(1024*1024,remaining))
                if not chunk: break
                remaining-=len(chunk)
            return self._json(415,{'error':'امتداد الملف غير مسموح.'})
        folder=DATA/'00_Quarantine'/target_type.lower(); folder.mkdir(parents=True,exist_ok=True)
        stored=f'q_{secrets.token_hex(16)}{suffix}'; dest=(folder/stored).resolve(); qroot=(DATA/'00_Quarantine').resolve()
        if qroot not in dest.parents: return self._json(400,{'error':'مسار الحجر غير آمن.'})
        first=b''; digest=hashlib.sha256(); remaining=length
        try:
            with open(dest,'xb') as out:
                while remaining>0:
                    chunk=self.rfile.read(min(1024*1024,remaining))
                    if not chunk: raise OSError('اكتمل تدفق الرفع قبل اكتمال الملف.')
                    if len(first)<64: first += chunk[:64-len(first)]
                    digest.update(chunk); out.write(chunk); remaining-=len(chunk)
            if not media_signature_ok(kind,suffix,first):
                dest.unlink(missing_ok=True)
                return self._json(415,{'error':'محتوى الملف لا يطابق توقيع نوع الصورة/الفيديو المسموح.'})
            os.chmod(dest,0o600); rel=dest.relative_to(DATA.resolve()).as_posix()
            with dbconn() as c:
                cur=c.execute("INSERT INTO security_quarantine(target_type,target_id,store_id,uploader_user_id,media_kind,original_name,stored_path,size_bytes,sha256,declared_content_type,scan_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(target_type,target_id,store_id,user_id,kind,name,rel,length,digest.hexdigest(),ctype,'QUARANTINED',stamp()))
                qid=cur.lastrowid; audit(c,user_id,'MEDIA_QUARANTINED','security_quarantine',qid,f'target={target_type}:{target_id}; sha256={digest.hexdigest()}; bytes={length}')
            return self._json(202,{'ok':True,'quarantine_id':qid,'status':'QUARANTINED','sha256':digest.hexdigest(),'message':'تم استلام الملف في منطقة حجر خاصة؛ لن يظهر للعموم حتى ينجح الفحص ويوافق مدير النظام.'})
        except Exception:
            dest.unlink(missing_ok=True)
            raise
    def _cookie_session(self):
        raw=self.headers.get('Cookie','')
        m=re.search(r'(?:^|;\s*)najeeb_session=([A-Za-z0-9_-]+)',raw)
        if not m: return None
        sess=SESSIONS.get(m.group(1))
        if not sess or sess['exp'] < now_utc().timestamp():
            SESSIONS.pop(m.group(1),None); return None
        with dbconn() as c:
            user=c.execute('SELECT id,username,role,is_active,must_change_password FROM users WHERE id=?',(sess['uid'],)).fetchone()
        if not user or not user['is_active']: return None
        sess['exp']=now_utc().timestamp()+SESSION_TTL
        return {'token':m.group(1),'user':rowdict(user)}
    def _require_admin(self):
        s=self._cookie_session()
        if not s: self._json(401,{'error':'يلزم تسجيل الدخول إلى تطبيق الإدارة.'}); return None
        if s['user']['role'] != 'SUPER_ADMIN': self._json(403,{'error':'ليست لديك صلاحية لهذه العملية.'}); return None
        if s['user']['must_change_password'] and self.path.split('?',1)[0] not in ['/api/admin/change-password','/api/admin/me','/api/admin/logout']:
            self._json(428,{'error':'يجب تغيير كلمة المرور المؤقتة قبل المتابعة.','must_change_password':True}); return None
        return s
    def _require_merchant(self):
        s=self._cookie_session()
        if not s: self._json(401,{'error':'يلزم تسجيل دخول التاجر.'}); return None
        if s['user']['role']!='MERCHANT': self._json(403,{'error':'هذه الواجهة مخصصة لحسابات تجار قطع الغيار.'}); return None
        if s['user']['must_change_password'] and self.path.split('?',1)[0] not in ['/api/merchant/change-password','/api/merchant/me','/api/merchant/logout']:
            self._json(428,{'error':'يجب تغيير كلمة المرور المؤقتة قبل متابعة إدارة المتجر.','must_change_password':True}); return None
        with dbconn() as c:
            store=c.execute('SELECT id,store_key,slug,display_name,status,contact_name,contact_phone,contact_email FROM merchant_stores WHERE owner_user_id=?',(s['user']['id'],)).fetchone()
        if not store: self._json(403,{'error':'هذا الحساب غير مرتبط بمتجر قطع غيار؛ راجع مدير النجيب.'}); return None
        s['store']=rowdict(store)
        return s
    def do_GET(self):
        path=urlparse(self.path).path
        try:
            if path in ('/','/index.html'): return self._static(STATIC/'index.html')
            if path in ('/admin','/admin/','/admin/index.html'): return self._static(ADMIN/'index.html')
            if path in ('/merchant','/merchant/','/merchant/index.html'): return self._static(MERCHANT/'index.html')
            if path.startswith('/merchant/assets/'): return self._static(MERCHANT/'assets'/unquote(path[len('/merchant/assets/'):]))
            if path == '/manifest.webmanifest': return self._static(STATIC/'manifest.webmanifest','application/manifest+json')
            if path == '/admin-manifest.webmanifest': return self._static(ADMIN/'manifest.webmanifest','application/manifest+json')
            if path == '/sw.js': return self._static(STATIC/'sw.js','application/javascript; charset=utf-8',{'Service-Worker-Allowed':'/'})
            if path == '/admin/sw.js': return self._static(ADMIN/'sw.js','application/javascript; charset=utf-8',{'Service-Worker-Allowed':'/admin/'})
            if path.startswith('/static/'): return self._static(STATIC / unquote(path[len('/static/'):]))
            if path.startswith('/admin/assets/'): return self._static(ADMIN / 'assets' / unquote(path[len('/admin/assets/'):]))
            if path.startswith('/media/'): return self._media(path[len('/media/'):])
            if path == '/api/health': return self._json(200,{'ok':True,'service':'Al-Najeeb pilot','database':'sqlite','utc':stamp(),'media_root':str(DATA)})
            if path == '/api/spare-parts/categories':
                with dbconn() as c: return self._json(200,{'categories':[rowdict(r) for r in c.execute('SELECT id,name_ar,slug FROM spare_part_categories WHERE active=1 ORDER BY id')]})
            if path == '/api/spare-parts':
                qs=parse_qs(urlparse(self.path).query); store_id=int(qs.get('store_id',['0'])[0] or 0)
                with dbconn() as c:
                    stores=[rowdict(r) for r in c.execute("SELECT id,slug,CASE WHEN status='APPROVED' THEN display_name WHEN store_key='merchant_1' THEN 'معرض التاجر الأول' WHEN store_key='merchant_2' THEN 'معرض التاجر الثاني' ELSE 'معرض التاجر الثالث' END AS display_name,CASE WHEN status='APPROVED' THEN 1 ELSE 0 END AS approved FROM merchant_stores ORDER BY id")]
                    rows=c.execute("SELECT p.id,p.store_id,p.category_id,p.name_ar,p.name_en,p.part_number,p.brand,p.compatible_equipment,p.model_range,p.specifications,p.application,p.condition,p.unit_price,p.currency,p.stock_quantity,p.description,p.image_path,p.video_path,p.updated_at,s.display_name AS store_name,c.name_ar AS category_name FROM spare_parts p JOIN merchant_stores s ON s.id=p.store_id JOIN spare_part_categories c ON c.id=p.category_id WHERE p.status='PUBLISHED' AND s.status='APPROVED' AND (?=0 OR p.store_id=?) ORDER BY p.updated_at DESC,p.id DESC",(store_id,store_id)).fetchall()
                    parts=[]
                    for r in rows:
                        d=rowdict(r); d['image_url']='/media/'+d['image_path'] if d['image_path'] else ''; d['video_url']='/media/'+d['video_path'] if d['video_path'] else ''; parts.append(d)
                return self._json(200,{'stores':stores,'parts':parts,'count':len(parts)})
            if path == '/api/catalog':
                with dbconn() as c:
                    expire_holds(c)
                    cats=[{k:rowdict(x)[k] for k in ('id','parent_id','name_ar','name_en')} for x in c.execute('SELECT id,parent_id,name_ar,name_en FROM categories ORDER BY parent_id,id')]
                    items=[]
                    for r in c.execute('SELECT p.*,c.name_ar AS category_name FROM products p LEFT JOIN categories c ON c.id=p.category_id WHERE p.active=1 ORDER BY p.created_at DESC,p.id DESC'):
                        d=rowdict(r); d['specs']=json.loads(d.pop('specs_json') or '{}'); d['image_url']='/media/'+d['image_path'] if d['image_path'] else ''; d['video_url']='/media/'+d['video_path'] if d['video_path'] else ''; d.pop('image_path',None); d.pop('video_path',None); d.pop('merchant_id',None); items.append(d)
                    ads=[rowdict(x) for x in c.execute('SELECT slot_key,title,subtitle,target_url,active FROM ad_slots ORDER BY id')]
                return self._json(200,{'categories':cats,'products':items,'ads':ads,'demo_mode':True})
            if path == '/api/admin/me':
                s=self._cookie_session()
                if not s: return self._json(200,{'authenticated':False})
                return self._json(200,{'authenticated':True,'user':s['user']})
            if path == '/api/admin/dashboard':
                s=self._require_admin()
                if not s:return
                with dbconn() as c:
                    expire_holds(c)
                    stats={
                      'products':c.execute('SELECT COUNT(*) n FROM products WHERE active=1').fetchone()['n'],
                      'categories':c.execute('SELECT COUNT(*) n FROM categories').fetchone()['n'],
                      'holds':c.execute("SELECT COUNT(*) n FROM reservations WHERE status='ACTIVE' AND expires_at>?",(stamp(),)).fetchone()['n'],
                      'quotes':c.execute("SELECT COUNT(*) n FROM quote_requests WHERE status='NEW'").fetchone()['n'],
                      'logs':c.execute('SELECT COUNT(*) n FROM audit_log').fetchone()['n']}
                    res=[rowdict(r) for r in c.execute('SELECT r.id,r.product_id,r.customer_name,r.phone,r.note,r.status,r.created_at,r.expires_at,p.title_ar FROM reservations r JOIN products p ON p.id=r.product_id ORDER BY r.id DESC LIMIT 30')]
                    quotes=[rowdict(r) for r in c.execute('SELECT q.id,q.product_id,q.customer_name,q.phone,q.details,q.status,q.created_at,p.title_ar FROM quote_requests q JOIN products p ON p.id=q.product_id ORDER BY q.id DESC LIMIT 30')]
                return self._json(200,{'stats':stats,'reservations':res,'quotes':quotes})
            if path == '/api/admin/merchants':
                s=self._require_admin()
                if not s:return
                with dbconn() as c:
                    rows=c.execute('SELECT m.id,m.store_key,m.slug,m.display_name,m.owner_user_id,m.status,m.contact_name,m.contact_phone,m.contact_email,m.created_at,m.updated_at,u.username AS owner_username FROM merchant_stores m LEFT JOIN users u ON u.id=m.owner_user_id ORDER BY m.id').fetchall()
                return self._json(200,{'stores':[rowdict(r) for r in rows]})
            if path == '/api/admin/spare-parts':
                s=self._require_admin()
                if not s:return
                with dbconn() as c:
                    rows=c.execute('SELECT p.*,m.display_name AS store_name,m.status AS store_status,c.name_ar AS category_name,u.username AS created_by_username FROM spare_parts p JOIN merchant_stores m ON m.id=p.store_id JOIN spare_part_categories c ON c.id=p.category_id LEFT JOIN users u ON u.id=p.created_by ORDER BY p.updated_at DESC,p.id DESC').fetchall()
                return self._json(200,{'parts':[rowdict(r) for r in rows]})
            if path == '/api/admin/spare-part-quotes':
                s=self._require_admin()
                if not s:return
                with dbconn() as c:
                    rows=c.execute('SELECT q.*,p.name_ar AS part_name,m.display_name AS store_name FROM spare_part_quote_requests q JOIN spare_parts p ON p.id=q.part_id JOIN merchant_stores m ON m.id=q.store_id ORDER BY q.id DESC LIMIT 500').fetchall()
                return self._json(200,{'quotes':[rowdict(r) for r in rows]})
            if path == '/api/merchant/me':
                s=self._cookie_session()
                if not s or s['user']['role']!='MERCHANT': return self._json(200,{'authenticated':False})
                with dbconn() as c:
                    store=c.execute('SELECT id,store_key,slug,display_name,status,contact_name,contact_phone,contact_email FROM merchant_stores WHERE owner_user_id=?',(s['user']['id'],)).fetchone()
                    count=c.execute('SELECT COUNT(*) n FROM spare_parts WHERE store_id=?',(store['id'],)).fetchone()['n'] if store else 0
                return self._json(200,{'authenticated':True,'user':s['user'],'store':rowdict(store),'parts_count':count})
            if path == '/api/merchant/spare-parts':
                s=self._require_merchant()
                if not s:return
                with dbconn() as c:
                    rows=c.execute('SELECT p.*,c.name_ar AS category_name FROM spare_parts p JOIN spare_part_categories c ON c.id=p.category_id WHERE p.store_id=? ORDER BY p.updated_at DESC,p.id DESC',(s['store']['id'],)).fetchall()
                return self._json(200,{'store':s['store'],'parts':[rowdict(r) for r in rows]})
            if path == '/api/admin/categories':
                s=self._require_admin()
                if not s:return
                with dbconn() as c: return self._json(200,{'categories':[rowdict(r) for r in c.execute('SELECT * FROM categories ORDER BY parent_id,id')]})
            if path == '/api/admin/products':
                s=self._require_admin()
                if not s:return
                with dbconn() as c: return self._json(200,{'products':[rowdict(r) for r in c.execute('SELECT p.*,c.name_ar category_name FROM products p LEFT JOIN categories c ON c.id=p.category_id ORDER BY p.id DESC')]})
            if path == '/api/admin/ads':
                s=self._require_admin()
                if not s:return
                with dbconn() as c: return self._json(200,{'ads':[rowdict(r) for r in c.execute('SELECT * FROM ad_slots ORDER BY id')]})
            if path == '/api/admin/backup/status':
                s=self._require_admin()
                if not s:return
                state=backup_status(BACKUP_ROOT)
                state['auto_backup_enabled']=os.environ.get('NAJEEB_AUTO_BACKUP','1').lower() not in ('0','false','no','off')
                try: state['daily_hour_local']=int(os.environ.get('NAJEEB_BACKUP_HOUR','2'))
                except ValueError: state['daily_hour_local']=2
                state['s3_upload_configured']=s3_is_configured()
                state['telegram_notify_configured']=telegram_is_configured()
                state['email_notify_configured']=email_is_configured()
                state['readable_reports_note']='تقارير CSV مقروءة تحفظ محليًا بصلاحيات مقيدة؛ لا تُرسل تلقائيًا إلى Telegram أو Gmail.'
                return self._json(200,state)
            if path == '/api/admin/ledger/accounts':
                s=self._require_admin()
                if not s:return
                with dbconn() as c:
                    accts=[rowdict(r) for r in c.execute('SELECT a.*,m.display_name AS store_name FROM financial_accounts a LEFT JOIN merchant_stores m ON m.id=a.store_id WHERE a.active=1 ORDER BY a.code')]
                    bal=c.execute("SELECT e.account_id,e.currency,SUM(CASE WHEN e.side='DEBIT' THEN e.amount_minor ELSE 0 END) AS debits,SUM(CASE WHEN e.side='CREDIT' THEN e.amount_minor ELSE 0 END) AS credits FROM ledger_entries e JOIN ledger_transactions t ON t.id=e.transaction_id WHERE t.status='POSTED' GROUP BY e.account_id,e.currency").fetchall()
                balances={}
                for b in bal:
                    # Signed values follow each account's normal balance side.
                    acct=next((a for a in accts if a['id']==b['account_id']),None)
                    if not acct:continue
                    normal_debit=acct['account_type'] in ('ASSET','EXPENSE')
                    amount=(b['debits']-b['credits']) if normal_debit else (b['credits']-b['debits'])
                    balances.setdefault(str(b['account_id']),{})[b['currency']]=amount
                for a in accts:a['balances_minor']=balances.get(str(a['id']),{})
                return self._json(200,{'accounts':accts,'currency_minor_unit':100,'message':'الأرصدة مشتقة من القيود؛ لا يوجد حقل لتعديل الرصيد مباشرة.'})
            if path == '/api/admin/ledger/transactions':
                s=self._require_admin()
                if not s:return
                with dbconn() as c:
                    txns=[rowdict(r) for r in c.execute('SELECT * FROM ledger_transactions ORDER BY id DESC LIMIT 200')]
                    entries=[rowdict(r) for r in c.execute("SELECT e.*,a.code AS account_code,a.name_ar AS account_name,a.account_type,a.store_id FROM ledger_entries e JOIN financial_accounts a ON a.id=e.account_id WHERE e.transaction_id IN (SELECT id FROM ledger_transactions ORDER BY id DESC LIMIT 200) ORDER BY e.transaction_id DESC,e.id")]
                grouped={}
                for e in entries:grouped.setdefault(str(e['transaction_id']),[]).append(e)
                for t in txns:t['entries']=grouped.get(str(t['id']),[])
                return self._json(200,{'transactions':txns})
            if path == '/api/merchant/ledger/transactions':
                s=self._require_merchant()
                if not s:return
                sid=s['store']['id']
                with dbconn() as c:
                    rows=c.execute("SELECT t.id,t.txn_key,t.txn_type,t.reference_type,t.reference_id,t.description,t.status,t.created_at,e.side,e.amount_minor,e.currency,e.memo,a.code AS account_code,a.name_ar AS account_name FROM ledger_transactions t JOIN ledger_entries e ON e.transaction_id=t.id JOIN financial_accounts a ON a.id=e.account_id WHERE t.status='POSTED' AND a.store_id=? ORDER BY t.id DESC,e.id LIMIT 500",(sid,)).fetchall()
                return self._json(200,{'entries':[rowdict(r) for r in rows],'store_id':sid,'read_only':True})
            if path == '/api/admin/security/quarantine':
                s=self._require_admin()
                if not s:return
                with dbconn() as c:
                    rows=c.execute("SELECT q.id,q.target_type,q.target_id,q.store_id,q.uploader_user_id,q.media_kind,q.original_name,q.size_bytes,q.sha256,q.declared_content_type,q.scan_status,q.scan_engine,q.scan_detail,q.created_at,q.scanned_at,q.released_at,COALESCE(sp.name_ar,p.title_ar,'عنصر غير معروف') AS target_name,COALESCE(ms.display_name,'معرض النجيب') AS store_name FROM security_quarantine q LEFT JOIN products p ON q.target_type='PRODUCT' AND p.id=q.target_id LEFT JOIN spare_parts sp ON q.target_type='SPARE_PART' AND sp.id=q.target_id LEFT JOIN merchant_stores ms ON ms.id=q.store_id ORDER BY q.id DESC LIMIT 500").fetchall()
                return self._json(200,{'items':[rowdict(r) for r in rows],'scanner_available':bool(shutil.which('clamscan')),'publication_note':'كل الملفات الواردة حجر افتراضيًا؛ لا تُنشر قبل فحص ناجح واعتماد يدوي.'})
            if path == '/api/admin/logs':
                s=self._require_admin()
                if not s:return
                with dbconn() as c: return self._json(200,{'logs':[rowdict(r) for r in c.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 200')]})
            if path == '/api/admin/storage/files':
                s=self._require_admin()
                if not s:return
                files=[]
                if DATA.exists():
                    for fp in DATA.rglob('*'):
                        if fp.is_file() and not fp.is_symlink():
                            rp=fp.resolve()
                            if DATA not in rp.parents: continue
                            st=rp.stat(); rel=rp.relative_to(DATA).as_posix()
                            files.append({'path':rel,'size':st.st_size,'modified_at':datetime.fromtimestamp(st.st_mtime,timezone.utc).isoformat().replace('+00:00','Z'),'content_type':mimetypes.guess_type(str(rp))[0] or 'application/octet-stream'})
                            if len(files)>=2000:break
                files.sort(key=lambda x:x['path'].lower())
                return self._json(200,{'root':str(DATA),'files':files,'truncated':len(files)>=2000})
            return self._json(404,{'error':'المسار غير موجود.'})
        except Exception as e:
            return self._json(500,{'error':'حدث خطأ داخلي.','detail':str(e) if os.environ.get('NAJEEB_DEBUG')=='1' else ''})
    def do_POST(self):
        path=urlparse(self.path).path
        try:
            if path == '/api/admin/merchants':
                s=self._require_admin()
                if not s:return
                d=self._body_json(); store_id=int(d.get('store_id',0)); username=str(d.get('username','')).strip(); password=str(d.get('temporary_password',''))
                display_name=str(d.get('display_name','')).strip(); contact_name=str(d.get('contact_name','')).strip(); phone=str(d.get('contact_phone','')).strip(); email=str(d.get('contact_email','')).strip()
                if not store_id or len(username)<3 or len(password)<10 or not display_name:
                    return self._json(400,{'error':'اختر أحد المعارض الثلاثة، وأدخل اسم العرض واسم مستخدم وكلمة مرور مؤقتة لا تقل عن 10 خانات.'})
                if not re.fullmatch(r'[A-Za-z0-9_.@+\-]{3,80}',username): return self._json(400,{'error':'اسم المستخدم يجب أن يحتوي حروفًا إنجليزية/أرقامًا أو . _ @ + - فقط.'})
                with dbconn() as c:
                    store=c.execute('SELECT * FROM merchant_stores WHERE id=?',(store_id,)).fetchone()
                    if not store:return self._json(404,{'error':'المعرض غير موجود؛ المنظومة التجريبية تدعم ثلاثة معارض محددة.'})
                    if store['owner_user_id'] is not None:return self._json(409,{'error':'هذا المعرض مرتبط بالفعل بحساب تاجر.'})
                    try:
                        c.execute('INSERT INTO users(username,password_hash,role,is_active,must_change_password,created_at) VALUES(?,?,?,?,?,?)',(username,password_hash(password),'MERCHANT',1,1,stamp()))
                        uid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']
                        c.execute('UPDATE merchant_stores SET owner_user_id=?,display_name=?,contact_name=?,contact_phone=?,contact_email=?,status=?,updated_at=? WHERE id=?',(uid,display_name,contact_name,phone,email,'PENDING_REVIEW',stamp(),store_id))
                        audit(c,s['user']['id'],'MERCHANT_ACCOUNT_CREATED','merchant_store',store_id,f'user={username}; approval=pending')
                    except sqlite3.IntegrityError:return self._json(409,{'error':'اسم المستخدم مستخدم بالفعل أو تعذر ربط الحساب.'})
                return self._json(201,{'ok':True,'store_id':store_id,'user_id':uid,'username':username,'status':'PENDING_REVIEW','message':'تم إنشاء حساب مؤقت؛ يلزم أن يغير التاجر كلمة المرور، ومحتواه لا يظهر قبل المراجعة والاعتماد.'})
            if path.startswith('/api/admin/merchants/') and path.endswith('/status'):
                s=self._require_admin()
                if not s:return
                store_id=int(path.strip('/').split('/')[3]); d=self._body_json(); status=str(d.get('status',''))
                if status not in ('PENDING_REVIEW','APPROVED','SUSPENDED'): return self._json(400,{'error':'حالة المتجر غير صالحة.'})
                with dbconn() as c:
                    store=c.execute('SELECT owner_user_id FROM merchant_stores WHERE id=?',(store_id,)).fetchone()
                    if not store:return self._json(404,{'error':'المعرض غير موجود.'})
                    if status=='APPROVED' and not store['owner_user_id']: return self._json(409,{'error':'لا يمكن اعتماد معرض قبل ربطه بحساب تاجر.'})
                    c.execute('UPDATE merchant_stores SET status=?,updated_at=? WHERE id=?',(status,stamp(),store_id))
                    audit(c,s['user']['id'],'MERCHANT_STORE_STATUS_CHANGED','merchant_store',store_id,status)
                return self._json(200,{'ok':True,'store_id':store_id,'status':status})
            if path.startswith('/api/admin/spare-parts/') and path.endswith('/review'):
                s=self._require_admin()
                if not s:return
                part_id=int(path.strip('/').split('/')[3]); d=self._body_json(); decision=str(d.get('decision','')); note=str(d.get('note','')).strip()[:1000]
                if decision not in ('approve','reject'): return self._json(400,{'error':'قرار المراجعة يجب أن يكون approve أو reject.'})
                with dbconn() as c:
                    part=c.execute('SELECT p.store_id,m.status AS store_status FROM spare_parts p JOIN merchant_stores m ON m.id=p.store_id WHERE p.id=?',(part_id,)).fetchone()
                    if not part:return self._json(404,{'error':'قطعة الغيار غير موجودة.'})
                    if decision=='approve' and part['store_status']!='APPROVED':return self._json(409,{'error':'لا يمكن نشر قطعة قبل اعتماد معرض التاجر.'})
                    status='PUBLISHED' if decision=='approve' else 'REJECTED'
                    c.execute('UPDATE spare_parts SET status=?,review_note=?,updated_at=? WHERE id=?',(status,note,stamp(),part_id))
                    audit(c,s['user']['id'],'SPARE_PART_REVIEWED','spare_part',part_id,f'{status}; {note}')
                return self._json(200,{'ok':True,'status':status})
            if path == '/api/merchant/spare-parts':
                s=self._require_merchant()
                if not s:return
                d=self._body_json(); category_id=int(d.get('category_id',0)); name=str(d.get('name_ar','')).strip(); name_en=str(d.get('name_en','')).strip(); part_no=str(d.get('part_number','')).strip(); brand=str(d.get('brand','')).strip(); compatible=str(d.get('compatible_equipment','')).strip(); model_range=str(d.get('model_range','')).strip(); specs=str(d.get('specifications','')).strip(); application=str(d.get('application','')).strip(); cond=str(d.get('condition','NEW')); price=float(d.get('unit_price',0)); currency=str(d.get('currency','SAR')); stock=int(d.get('stock_quantity',0)); desc=str(d.get('description','')).strip()
                if not name or len(name)>200 or price<0 or stock<0 or category_id<1:return self._json(400,{'error':'اسم القطعة والقسم والسعر والمخزون مطلوبة بقيم صحيحة.'})
                if cond not in ('NEW','USED','REFURBISHED','UNKNOWN'):return self._json(400,{'error':'حالة القطعة غير صالحة.'})
                if currency not in ('SAR','YER','USD'):return self._json(400,{'error':'العملة غير مدعومة في نسخة الاختبار.'})
                with dbconn() as c:
                    if not c.execute('SELECT id FROM spare_part_categories WHERE id=? AND active=1',(category_id,)).fetchone():return self._json(400,{'error':'قسم قطع الغيار غير صالح.'})
                    c.execute('INSERT INTO spare_parts(store_id,category_id,name_ar,name_en,part_number,brand,compatible_equipment,model_range,specifications,application,condition,unit_price,currency,stock_quantity,description,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(s['store']['id'],category_id,name,name_en,part_no,brand,compatible,model_range,specs,application,cond,price,currency,stock,desc,'PENDING_REVIEW',s['user']['id'],stamp(),stamp()))
                    part_id=c.execute('SELECT last_insert_rowid() id').fetchone()['id']; audit(c,s['user']['id'],'SPARE_PART_SUBMITTED','spare_part',part_id,'pending review')
                return self._json(201,{'ok':True,'id':part_id,'status':'PENDING_REVIEW','message':'تم حفظ القطعة وهي بانتظار مراجعة مدير النجيب.'})
            if path.startswith('/api/merchant/spare-parts/') and path.endswith('/edit'):
                s=self._require_merchant()
                if not s:return
                part_id=int(path.strip('/').split('/')[3]); d=self._body_json(); category_id=int(d.get('category_id',0)); name=str(d.get('name_ar','')).strip(); price=float(d.get('unit_price',0)); stock=int(d.get('stock_quantity',0))
                if not name or price<0 or stock<0:return self._json(400,{'error':'تحقق من اسم القطعة والسعر والمخزون.'})
                with dbconn() as c:
                    exists=c.execute('SELECT id FROM spare_parts WHERE id=? AND store_id=?',(part_id,s['store']['id'])).fetchone()
                    if not exists:return self._json(404,{'error':'قطعة الغيار غير موجودة في معرضك.'})
                    if not c.execute('SELECT id FROM spare_part_categories WHERE id=? AND active=1',(category_id,)).fetchone():return self._json(400,{'error':'قسم القطعة غير صالح.'})
                    c.execute('UPDATE spare_parts SET category_id=?,name_ar=?,name_en=?,part_number=?,brand=?,compatible_equipment=?,model_range=?,specifications=?,application=?,condition=?,unit_price=?,currency=?,stock_quantity=?,description=?,status=?,updated_at=? WHERE id=? AND store_id=?',(category_id,name,str(d.get('name_en','')).strip(),str(d.get('part_number','')).strip(),str(d.get('brand','')).strip(),str(d.get('compatible_equipment','')).strip(),str(d.get('model_range','')).strip(),str(d.get('specifications','')).strip(),str(d.get('application','')).strip(),str(d.get('condition','NEW')),price,str(d.get('currency','SAR')),stock,str(d.get('description','')).strip(),'PENDING_REVIEW',stamp(),part_id,s['store']['id']))
                    audit(c,s['user']['id'],'SPARE_PART_UPDATED','spare_part',part_id,'resubmitted to review')
                return self._json(200,{'ok':True,'status':'PENDING_REVIEW'})
            if path.startswith('/api/merchant/spare-parts/') and path.endswith('/archive'):
                s=self._require_merchant()
                if not s:return
                part_id=int(path.strip('/').split('/')[3])
                with dbconn() as c:
                    cur=c.execute('UPDATE spare_parts SET status=?,updated_at=? WHERE id=? AND store_id=?',('ARCHIVED',stamp(),part_id,s['store']['id']))
                    if cur.rowcount==0:return self._json(404,{'error':'قطعة الغيار غير موجودة في معرضك.'})
                    audit(c,s['user']['id'],'SPARE_PART_ARCHIVED','spare_part',part_id,'merchant archive')
                return self._json(200,{'ok':True})
            if path == '/api/spare-part-quotes':
                d=self._body_json(); part_id=int(d.get('part_id',0)); name=str(d.get('customer_name','')).strip(); phone=str(d.get('phone','')).strip(); details=str(d.get('details','')).strip()
                if not name or not phone or len(name)>120 or len(phone)>40:return self._json(400,{'error':'الاسم ورقم التواصل مطلوبان.'})
                with dbconn() as c:
                    part=c.execute('SELECT p.id,p.store_id FROM spare_parts p JOIN merchant_stores m ON m.id=p.store_id WHERE p.id=? AND p.status=? AND m.status=?',(part_id,'PUBLISHED','APPROVED')).fetchone()
                    if not part:return self._json(404,{'error':'قطعة الغيار غير متاحة.'})
                    c.execute('INSERT INTO spare_part_quote_requests(part_id,store_id,customer_name,phone,details,created_at) VALUES(?,?,?,?,?,?)',(part_id,part['store_id'],name,phone,details,stamp()))
                    qid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']; audit(c,None,'SPARE_PART_QUOTE_REQUESTED','spare_part_quote',qid,f'part={part_id}')
                return self._json(201,{'ok':True,'id':qid,'message':'تم إرسال طلبك إلى معرض قطع الغيار المختص.'})
            if path == '/api/admin/backup/create':
                s=self._require_admin()
                if not s:return
                if not BACKUP_LOCK.acquire(blocking=False): return self._json(409,{'error':'هناك عملية نسخ احتياطي أخرى قيد التنفيذ.'})
                try:
                    result=create_backup(DB,DATA,BACKUP_ROOT,retain_days=int(os.environ.get('NAJEEB_BACKUP_RETAIN_DAYS','90')))
                    try: result['cloud_delivery']=upload_s3(Path(result['path']))
                    except Exception as cloud_err: result['cloud_delivery']={'configured':True,'uploaded':False,'reason':str(cloud_err)[:300]}
                    try: result['notification']=notify_backup(result,result.get('cloud_delivery'))
                    except Exception as notify_err: result['notification']={'sent':False,'reason':str(notify_err)[:300]}
                    with dbconn() as c: audit(c,s['user']['id'],'BACKUP_CREATED','backup',result['file'],f"sha256={result['sha256']}; bytes={result['size_bytes']}; cloud={result.get('cloud_delivery',{}).get('uploaded',False)}")
                    return self._json(201,result)
                finally: BACKUP_LOCK.release()
            if path == '/api/merchant/login':
                d=self._body_json(); username=str(d.get('username','')).strip(); password=str(d.get('password',''))
                with dbconn() as c:
                    u=c.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
                    if not u or not u['is_active'] or u['role']!='MERCHANT' or not password_verify(password,u['password_hash']):
                        if u: audit(c,u['id'],'MERCHANT_LOGIN_FAILED','user',u['id'],'bad credentials or role')
                        return self._json(401,{'error':'بيانات دخول التاجر غير صحيحة.'})
                    store=c.execute('SELECT id,status FROM merchant_stores WHERE owner_user_id=?',(u['id'],)).fetchone()
                    if not store: return self._json(403,{'error':'حساب التاجر غير مرتبط بمتجر.'})
                    if store['status']=='SUSPENDED': return self._json(403,{'error':'تم تعليق المتجر؛ راجع مدير النجيب.'})
                    token=secrets.token_urlsafe(32); SESSIONS[token]={'uid':u['id'],'exp':now_utc().timestamp()+SESSION_TTL}
                    audit(c,u['id'],'MERCHANT_LOGIN','merchant_store',store['id'],'success')
                    cookie=f'najeeb_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}' + ('; Secure' if self.headers.get('X-Forwarded-Proto')=='https' else '')
                    return self._json(200,{'ok':True,'must_change_password':bool(u['must_change_password']),'user':{'username':u['username'],'role':u['role']}},{'Set-Cookie':cookie})
            if path == '/api/admin/login':
                d=self._body_json(); username=str(d.get('username','')).strip(); password=str(d.get('password',''))
                with dbconn() as c:
                    u=c.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
                    if not u or not u['is_active'] or not password_verify(password,u['password_hash']):
                        if u: audit(c,u['id'],'LOGIN_FAILED','user',u['id'],'bad credentials')
                        return self._json(401,{'error':'اسم المستخدم أو كلمة المرور غير صحيحة.'})
                    if u['role']!='SUPER_ADMIN': return self._json(403,{'error':'هذه نسخة تجريبية مقصورة على مدير النظام.'})
                    token=secrets.token_urlsafe(32); SESSIONS[token]={'uid':u['id'],'exp':now_utc().timestamp()+SESSION_TTL}
                    audit(c,u['id'],'LOGIN','user',u['id'],'success')
                    cookie=f'najeeb_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}' + ('; Secure' if self.headers.get('X-Forwarded-Proto')=='https' else '')
                    return self._json(200,{'ok':True,'must_change_password':bool(u['must_change_password']),'user':{'username':u['username'],'role':u['role']}},{'Set-Cookie':cookie})
            if path in ('/api/admin/logout','/api/merchant/logout'):
                length=int(self.headers.get('Content-Length','0'))
                if length: self.rfile.read(min(length,1024*1024))
                s=self._cookie_session()
                if s: SESSIONS.pop(s['token'],None)
                return self._json(200,{'ok':True},{'Set-Cookie':'najeeb_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
            if path in ('/api/admin/change-password','/api/merchant/change-password'):
                s=self._cookie_session()
                if not s:return self._json(401,{'error':'سجّل الدخول أولًا.'})
                d=self._body_json(); newuser=str(d.get('username','')).strip(); password=str(d.get('password',''))
                if len(password)<8:return self._json(400,{'error':'يجب ألا تقل كلمة المرور عن 8 خانات.'})
                if not re.fullmatch(r'[\w.@+\-\u0600-\u06FF]{3,100}',newuser,re.UNICODE):return self._json(400,{'error':'اسم المستخدم غير صالح.'})
                with dbconn() as c:
                    try:c.execute('UPDATE users SET username=?,password_hash=?,must_change_password=0 WHERE id=?',(newuser,password_hash(password),s['user']['id']))
                    except sqlite3.IntegrityError:return self._json(409,{'error':'اسم المستخدم مستخدم بالفعل.'})
                    audit(c,s['user']['id'],'PASSWORD_ROTATED','user',s['user']['id'],'first login changed username/password')
                return self._json(200,{'ok':True,'message':'تم تغيير بيانات الدخول. سجّل الدخول بالبيانات الجديدة.'},{'Set-Cookie':'najeeb_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
            if path == '/api/reservations':
                d=self._body_json(); pid=int(d.get('product_id',0)); name=str(d.get('customer_name','')).strip(); phone=str(d.get('phone','')).strip(); note=str(d.get('note','')).strip()
                if not name or not phone:return self._json(400,{'error':'الاسم ورقم التواصل مطلوبان.'})
                if len(name)>120 or len(phone)>40:return self._json(400,{'error':'تجاوز أحد الحقول الحد المسموح.'})
                with dbconn() as c:
                    c.execute('BEGIN IMMEDIATE'); expire_holds(c)
                    p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
                    if not p:return self._json(404,{'error':'المعدة غير موجودة.'})
                    if p['availability']=='SOLD':return self._json(409,{'error':'المعدة مباعة وغير متاحة.'})
                    active=c.execute("SELECT id FROM reservations WHERE product_id=? AND status='ACTIVE' AND expires_at>?",(pid,stamp())).fetchone()
                    if active:return self._json(409,{'error':'المعدة محجوزة حاليًا. اختر معدة أخرى.'})
                    created=now_utc(); expiry=created+timedelta(hours=24)
                    c.execute('INSERT INTO reservations(product_id,customer_name,phone,note,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?)',(pid,name,phone,note,'ACTIVE',stamp(created),stamp(expiry)))
                    rid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']
                    c.execute("UPDATE products SET availability='HOLD_24H',updated_at=? WHERE id=?",(stamp(),pid))
                    audit(c,None,'CUSTOMER_HOLD_CREATED','reservation',rid,f'product={pid};expires={stamp(expiry)}')
                return self._json(201,{'ok':True,'reservation_id':rid,'expires_at':stamp(expiry),'message':'تم تسجيل حجز تجريبي لمدة 24 ساعة على قاعدة البيانات المحلية. لا يوجد دفع إلكتروني.'})
            if path == '/api/quotes':
                d=self._body_json(); pid=int(d.get('product_id',0)); name=str(d.get('customer_name','')).strip(); phone=str(d.get('phone','')).strip(); details=str(d.get('details','')).strip()
                if not name or not phone:return self._json(400,{'error':'الاسم ورقم التواصل مطلوبان.'})
                with dbconn() as c:
                    p=c.execute('SELECT id FROM products WHERE id=? AND active=1',(pid,)).fetchone()
                    if not p:return self._json(404,{'error':'المعدة غير موجودة.'})
                    c.execute('INSERT INTO quote_requests(product_id,customer_name,phone,details,created_at) VALUES(?,?,?,?,?)',(pid,name,phone,details,stamp()))
                    rid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']; audit(c,None,'QUOTE_REQUEST_CREATED','quote',rid,f'product={pid}')
                return self._json(201,{'ok':True,'request_id':rid,'message':'تم حفظ طلب عرض السعر في قاعدة البيانات المحلية التجريبية.'})
            if path == '/api/admin/merchants':
                s=self._require_admin()
                if not s:return
                with dbconn() as c:
                    rows=c.execute('SELECT m.id,m.store_key,m.slug,m.display_name,m.owner_user_id,m.status,m.contact_name,m.contact_phone,m.contact_email,m.created_at,m.updated_at,u.username AS owner_username FROM merchant_stores m LEFT JOIN users u ON u.id=m.owner_user_id ORDER BY m.id').fetchall()
                return self._json(200,{'stores':[rowdict(r) for r in rows]})
            if path == '/api/admin/spare-parts':
                s=self._require_admin()
                if not s:return
                with dbconn() as c:
                    rows=c.execute('SELECT p.*,m.display_name AS store_name,m.status AS store_status,c.name_ar AS category_name,u.username AS created_by_username FROM spare_parts p JOIN merchant_stores m ON m.id=p.store_id JOIN spare_part_categories c ON c.id=p.category_id LEFT JOIN users u ON u.id=p.created_by ORDER BY p.updated_at DESC,p.id DESC').fetchall()
                return self._json(200,{'parts':[rowdict(r) for r in rows]})
            if path == '/api/admin/spare-part-quotes':
                s=self._require_admin()
                if not s:return
                with dbconn() as c:
                    rows=c.execute('SELECT q.*,p.name_ar AS part_name,m.display_name AS store_name FROM spare_part_quote_requests q JOIN spare_parts p ON p.id=q.part_id JOIN merchant_stores m ON m.id=q.store_id ORDER BY q.id DESC LIMIT 500').fetchall()
                return self._json(200,{'quotes':[rowdict(r) for r in rows]})
            if path == '/api/merchant/me':
                s=self._cookie_session()
                if not s or s['user']['role']!='MERCHANT': return self._json(200,{'authenticated':False})
                with dbconn() as c:
                    store=c.execute('SELECT id,store_key,slug,display_name,status,contact_name,contact_phone,contact_email FROM merchant_stores WHERE owner_user_id=?',(s['user']['id'],)).fetchone()
                    count=c.execute('SELECT COUNT(*) n FROM spare_parts WHERE store_id=?',(store['id'],)).fetchone()['n'] if store else 0
                return self._json(200,{'authenticated':True,'user':s['user'],'store':rowdict(store),'parts_count':count})
            if path == '/api/merchant/spare-parts':
                s=self._require_merchant()
                if not s:return
                with dbconn() as c:
                    rows=c.execute('SELECT p.*,c.name_ar AS category_name FROM spare_parts p JOIN spare_part_categories c ON c.id=p.category_id WHERE p.store_id=? ORDER BY p.updated_at DESC,p.id DESC',(s['store']['id'],)).fetchall()
                return self._json(200,{'store':s['store'],'parts':[rowdict(r) for r in rows]})
            if path == '/api/admin/categories':
                s=self._require_admin()
                if not s:return
                d=self._body_json(); name=str(d.get('name_ar','')).strip(); en=str(d.get('name_en','')).strip(); parent=d.get('parent_id') or None; folder=str(d.get('folder_path','')).strip()
                if not name:return self._json(400,{'error':'اسم القسم مطلوب.'})
                if not folder:folder='01_Categories_Media/'+safe_filename(name.replace(' ','_'))
                folder=folder.replace('\\','/').strip('/')
                if any(part in ('..','.') for part in Path(folder).parts) or Path(folder).is_absolute() or not folder:
                    return self._json(400,{'error':'مسار مجلد غير مسموح؛ استخدم مسارًا نسبيًا داخل جذر التخزين.'})
                folder_target=(DATA/folder).resolve()
                if DATA not in folder_target.parents:return self._json(400,{'error':'المجلد المطلوب يخرج من جذر التخزين.'})
                with dbconn() as c:
                    if parent and not c.execute('SELECT id FROM categories WHERE id=?',(parent,)).fetchone():return self._json(400,{'error':'القسم الأب غير موجود.'})
                    cur=c.execute('INSERT INTO categories(parent_id,name_ar,name_en,folder_path,created_at) VALUES(?,?,?,?,?)',(parent,name,en,folder,stamp())); cid=cur.lastrowid
                    (DATA/folder).mkdir(parents=True,exist_ok=True); audit(c,s['user']['id'],'CATEGORY_CREATED','category',cid,name)
                return self._json(201,{'ok':True,'id':cid})
            if path == '/api/admin/products':
                s=self._require_admin()
                if not s:return
                d=self._body_json(); title=str(d.get('title_ar','')).strip(); cat=int(d.get('category_id',0)); price=float(d.get('price',0)); cond=str(d.get('condition','USED_CLEAN')); brand=str(d.get('brand','')).strip(); desc=str(d.get('description','')).strip(); title_en=str(d.get('title_en','')).strip()
                if not title or price<0:return self._json(400,{'error':'اسم المعدة والسعر الصحيح مطلوبان.'})
                if cond not in {'NEW_4K','NEAR_NEW_BLURAY','USED_CLEAN','REFURBISHED','READY_NO_MAINTENANCE'}:return self._json(400,{'error':'حالة المعدة غير صالحة.'})
                with dbconn() as c:
                    if not c.execute('SELECT id FROM categories WHERE id=?',(cat,)).fetchone():return self._json(400,{'error':'اختر قسمًا صحيحًا.'})
                    cur=c.execute('INSERT INTO products(category_id,title_ar,title_en,brand,price,condition,description,specs_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(cat,title,title_en,brand,price,cond,desc,json.dumps(d.get('specs') or {},ensure_ascii=False),stamp(),stamp())); pid=cur.lastrowid
                    audit(c,s['user']['id'],'PRODUCT_CREATED','product',pid,title)
                return self._json(201,{'ok':True,'id':pid})
            if path == '/api/admin/ledger/post':
                s=self._require_admin()
                if not s:return
                d=self._body_json(); tx_type=str(d.get('txn_type','ADJUSTMENT')).upper(); description=str(d.get('description','')).strip()[:1000]
                ref_type=str(d.get('reference_type','')).strip()[:80]; ref_id=str(d.get('reference_id','')).strip()[:120]; lines=d.get('entries')
                allowed_types={'SALE','DEPOSIT','REFUND','COMMISSION','EXPENSE','ADJUSTMENT','REVERSAL','SETTLEMENT'}
                if tx_type not in allowed_types or not description or not isinstance(lines,list) or len(lines)<2 or len(lines)>100:
                    return self._json(400,{'error':'أدخل نوع حركة صحيحًا ووصفًا وقيدين على الأقل.'})
                normalized=[]; sums={}
                try:
                    for line in lines:
                        side=str(line.get('side','')).upper(); aid=int(line.get('account_id',0)); cur=str(line.get('currency','SAR')).upper(); amount=Decimal(str(line.get('amount',0)))
                        if side not in ('DEBIT','CREDIT') or cur not in ('SAR','YER','USD') or not amount.is_finite() or amount<=0:
                            return self._json(400,{'error':'راجع جهة القيد والعملة والمبلغ الموجب.'})
                        cents=amount*100
                        if cents != cents.to_integral_value(): return self._json(400,{'error':'المبلغ يجب ألا يتجاوز منزلتين عشريتين.'})
                        minor=int(cents); normalized.append((aid,side,minor,cur,str(line.get('memo','')).strip()[:500])); sums.setdefault(cur,{'DEBIT':0,'CREDIT':0})[side]+=minor
                except (InvalidOperation,TypeError,ValueError,AttributeError): return self._json(400,{'error':'تنسيق مبلغ أو قيد غير صالح.'})
                if any(v['DEBIT']!=v['CREDIT'] for v in sums.values()): return self._json(400,{'error':'القيد غير متوازن: يجب تساوي إجمالي المدين والدائن لكل عملة على حدة.'})
                with dbconn() as c:
                    c.execute('BEGIN IMMEDIATE')
                    for aid,side,minor,cur,memo in normalized:
                        if not c.execute('SELECT id FROM financial_accounts WHERE id=? AND active=1',(aid,)).fetchone(): return self._json(400,{'error':f'الحساب {aid} غير موجود أو غير نشط.'})
                    tx_key='NJB-'+secrets.token_hex(12).upper()
                    curx=c.execute('INSERT INTO ledger_transactions(txn_key,txn_type,reference_type,reference_id,description,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)',(tx_key,tx_type,ref_type,ref_id,description,'POSTED',s['user']['id'],stamp()))
                    tid=curx.lastrowid
                    for aid,side,minor,cur,memo in normalized:c.execute('INSERT INTO ledger_entries(transaction_id,account_id,side,amount_minor,currency,memo,created_at) VALUES(?,?,?,?,?,?,?)',(tid,aid,side,minor,cur,memo,stamp()))
                    audit(c,s['user']['id'],'LEDGER_TRANSACTION_POSTED','ledger_transaction',tid,f'{tx_key}; {tx_type}; lines={len(normalized)}')
                return self._json(201,{'ok':True,'id':tid,'txn_key':tx_key,'status':'POSTED','message':'تم ترحيل القيد المتوازن؛ لا يُعدّل أو يُحذف، والتصحيح يكون بقيد عكسي جديد.'})
            if path.startswith('/api/admin/security/quarantine/') and path.endswith('/scan'):
                s=self._require_admin()
                if not s:return
                qid=int(path.strip('/').split('/')[4])
                with dbconn() as c: row=c.execute('SELECT * FROM security_quarantine WHERE id=?',(qid,)).fetchone()
                if not row:return self._json(404,{'error':'عنصر الحجر غير موجود.'})
                src=(DATA/row['stored_path']).resolve(); qroot=(DATA/'00_Quarantine').resolve()
                if qroot not in src.parents or not src.is_file():return self._json(404,{'error':'ملف الحجر غير موجود أو مساره غير آمن.'})
                result=scan_with_clamav(src)
                with dbconn() as c:
                    c.execute('UPDATE security_quarantine SET scan_status=?,scan_engine=?,scan_detail=?,scanned_at=? WHERE id=?',(result['status'],result['engine'],result['detail'],stamp(),qid))
                    audit(c,s['user']['id'],'QUARANTINE_SCAN_COMPLETED','security_quarantine',qid,result['status']+'; '+result['engine'])
                return self._json(200,{'ok':True,'quarantine_id':qid,**result})
            if path.startswith('/api/admin/security/quarantine/') and path.endswith('/release'):
                s=self._require_admin()
                if not s:return
                qid=int(path.strip('/').split('/')[4])
                with dbconn() as c: row=c.execute('SELECT * FROM security_quarantine WHERE id=?',(qid,)).fetchone()
                if not row:return self._json(404,{'error':'عنصر الحجر غير موجود.'})
                if row['scan_status']!='CLEAN':return self._json(409,{'error':'لا يمكن نشر ملف لم يجتز فحص مضاد الفيروسات. يجب أن تكون حالة الفحص CLEAN.'})
                src=(DATA/row['stored_path']).resolve(); qroot=(DATA/'00_Quarantine').resolve()
                if qroot not in src.parents or not src.is_file():return self._json(404,{'error':'ملف الحجر غير موجود أو مساره غير آمن.'})
                suffix=src.suffix.lower()
                if row['target_type']=='PRODUCT': folder='03_Products_Images' if row['media_kind']=='image' else '02_Equipment_Videos'; basename=f"product_{row['target_id']}_{secrets.token_hex(8)}{suffix}"
                else: folder=f"04_Spare_Parts/Approved/merchant_{row['store_id']}"; basename=f"spare_{row['target_id']}_{secrets.token_hex(8)}{suffix}"
                destdir=DATA/folder; destdir.mkdir(parents=True,exist_ok=True); dest=(destdir/basename).resolve()
                if DATA.resolve() not in dest.parents:return self._json(400,{'error':'مسار النشر غير آمن.'})
                rel=dest.relative_to(DATA.resolve()).as_posix()
                try:
                    os.replace(src,dest)
                    with dbconn() as c:
                        col='image_path' if row['media_kind']=='image' else 'video_path'
                        if row['target_type']=='PRODUCT': cur=c.execute(f'UPDATE products SET {col}=?,updated_at=? WHERE id=?',(rel,stamp(),row['target_id']))
                        else: cur=c.execute(f'UPDATE spare_parts SET {col}=?,updated_at=? WHERE id=? AND store_id=?',(rel,stamp(),row['target_id'],row['store_id']))
                        if cur.rowcount!=1:
                            os.replace(dest,src)
                            return self._json(409,{'error':'لم يعد العنصر الهدف متاحًا؛ لم ينشر الملف.'})
                        c.execute("UPDATE security_quarantine SET scan_status='RELEASED',stored_path=?,released_at=? WHERE id=?",(rel,stamp(),qid))
                        audit(c,s['user']['id'],'QUARANTINE_MEDIA_RELEASED','security_quarantine',qid,rel)
                except Exception:
                    if dest.exists() and not src.exists():
                        try: os.replace(dest,src)
                        except OSError: pass
                    raise
                return self._json(200,{'ok':True,'status':'RELEASED','path':rel,'message':'اجتاز الملف الفحص ومراجعة مدير النظام. لا يظهر الملف إلا إذا كان المنتج/القطعة منشورًا ومعتمدًا.'})
            if path.startswith('/api/admin/security/quarantine/') and path.endswith('/reject'):
                s=self._require_admin()
                if not s:return
                qid=int(path.strip('/').split('/')[4]); d=self._body_json(); note=str(d.get('note','')).strip()[:1000]
                with dbconn() as c:
                    row=c.execute('SELECT id FROM security_quarantine WHERE id=?',(qid,)).fetchone()
                    if not row:return self._json(404,{'error':'عنصر الحجر غير موجود.'})
                    c.execute("UPDATE security_quarantine SET scan_status='REJECTED',scan_detail=?,scanned_at=? WHERE id=?",('رفض إداري: '+note,stamp(),qid))
                    audit(c,s['user']['id'],'QUARANTINE_MEDIA_REJECTED','security_quarantine',qid,note)
                return self._json(200,{'ok':True,'status':'REJECTED'})
            if path.startswith('/api/admin/products/') and path.endswith('/upload'):
                s=self._require_admin()
                if not s:return
                pid=int(path.strip('/').split('/')[3])
                with dbconn() as c: exists=c.execute('SELECT id FROM products WHERE id=?',(pid,)).fetchone()
                if not exists:
                    try: length=int(self.headers.get('Content-Length','0'))
                    except ValueError: length=0
                    while length>0:
                        chunk=self.rfile.read(min(1024*1024,length))
                        if not chunk:break
                        length-=len(chunk)
                    return self._json(404,{'error':'المعدة غير موجودة.'})
                return self._quarantine_upload('PRODUCT',pid,None,s['user']['id'])
            if path.startswith('/api/merchant/spare-parts/') and path.endswith('/upload'):
                s=self._require_merchant()
                if not s:return
                part_id=int(path.strip('/').split('/')[3])
                with dbconn() as c: part=c.execute('SELECT id FROM spare_parts WHERE id=? AND store_id=?',(part_id,s['store']['id'])).fetchone()
                if not part:
                    try: length=int(self.headers.get('Content-Length','0'))
                    except ValueError: length=0
                    while length>0:
                        chunk=self.rfile.read(min(1024*1024,length))
                        if not chunk:break
                        length-=len(chunk)
                    return self._json(404,{'error':'قطعة الغيار غير موجودة في معرضك.'})
                return self._quarantine_upload('SPARE_PART',part_id,s['store']['id'],s['user']['id'])
            if path.startswith('/api/admin/products/') and path.endswith('/edit'):
                s=self._require_admin()
                if not s:return
                pid=int(path.strip('/').split('/')[3]); d=self._body_json()
                title=str(d.get('title_ar','')).strip(); title_en=str(d.get('title_en','')).strip(); brand=str(d.get('brand','')).strip(); desc=str(d.get('description','')).strip(); price=float(d.get('price',0)); cond=str(d.get('condition','USED_CLEAN')); cat=int(d.get('category_id',0))
                if not title or price<0:return self._json(400,{'error':'اسم المعدة والسعر مطلوبان.'})
                if cond not in {'NEW_4K','NEAR_NEW_BLURAY','USED_CLEAN','REFURBISHED','READY_NO_MAINTENANCE'}:return self._json(400,{'error':'حالة المعدة غير صالحة.'})
                with dbconn() as c:
                    if not c.execute('SELECT id FROM categories WHERE id=?',(cat,)).fetchone():return self._json(400,{'error':'القسم غير موجود.'})
                    c.execute('UPDATE products SET category_id=?,title_ar=?,title_en=?,brand=?,price=?,condition=?,description=?,updated_at=? WHERE id=?',(cat,title,title_en,brand,price,cond,desc,stamp(),pid))
                    audit(c,s['user']['id'],'PRODUCT_UPDATED','product',pid,title)
                return self._json(200,{'ok':True})
            if path.startswith('/api/admin/products/') and path.endswith('/remove-media'):
                s=self._require_admin()
                if not s:return
                pid=int(path.strip('/').split('/')[3]); d=self._body_json(); kind=str(d.get('kind',''))
                if kind not in ('image','video'):return self._json(400,{'error':'نوع الوسيط غير صحيح.'})
                col='image_path' if kind=='image' else 'video_path'
                with dbconn() as c:
                    row=c.execute(f'SELECT {col} AS media FROM products WHERE id=?',(pid,)).fetchone()
                    if not row:return self._json(404,{'error':'المعدة غير موجودة.'})
                    rel=row['media']
                    if rel:
                        f=(DATA/rel).resolve()
                        if DATA not in f.parents:return self._json(400,{'error':'مسار الوسيط خارج الجذر.'})
                        if f.is_file():f.unlink()
                        c.execute(f"UPDATE products SET {col}=?,updated_at=? WHERE id=?",('',stamp(),pid))
                        audit(c,s['user']['id'],'PRODUCT_MEDIA_REMOVED','product',pid,rel)
                return self._json(200,{'ok':True})
            if path.startswith('/api/admin/products/') and path.endswith('/availability'):
                s=self._require_admin()
                if not s:return
                pid=int(path.strip('/').split('/')[3]); d=self._body_json(); status=str(d.get('availability','AVAILABLE'))
                if status not in ('AVAILABLE','SOLD'):return self._json(400,{'error':'يمكن تعيين متاح أو مباع يدويًا؛ الحجز يُدار من سجل الحجز.'})
                with dbconn() as c:
                    expire_holds(c)
                    active=c.execute("SELECT id FROM reservations WHERE product_id=? AND status='ACTIVE' AND expires_at>? LIMIT 1",(pid,stamp())).fetchone()
                    if active:return self._json(409,{'error':'لا يمكن تغيير توفر المعدة بينما لها حجز نشط؛ ألغِ الحجز أولًا.'})
                    c.execute('UPDATE products SET availability=?,updated_at=? WHERE id=?',(status,stamp(),pid)); audit(c,s['user']['id'],'PRODUCT_AVAILABILITY_CHANGED','product',pid,status)
                return self._json(200,{'ok':True})
            if path.startswith('/api/admin/reservations/') and path.endswith('/cancel'):
                d=self._body_json()
                s=self._require_admin()
                if not s:return
                rid=int(path.strip('/').split('/')[3])
                with dbconn() as c:
                    c.execute('UPDATE reservations SET status="CANCELLED" WHERE id=? AND status="ACTIVE"',(rid,)); p=c.execute('SELECT product_id FROM reservations WHERE id=?',(rid,)).fetchone()
                    if p: expire_holds(c); c.execute("UPDATE products SET availability='AVAILABLE' WHERE id=? AND availability='HOLD_24H' AND NOT EXISTS (SELECT 1 FROM reservations WHERE product_id=? AND status='ACTIVE' AND expires_at>?)",(p['product_id'],p['product_id'],stamp()))
                    audit(c,s['user']['id'],'RESERVATION_CANCELLED','reservation',rid,'admin action')
                return self._json(200,{'ok':True})
            if path == '/api/admin/storage/delete-file':
                s=self._require_admin()
                if not s:return
                d=self._body_json(); rel=str(d.get('path','')).replace('\\','/').lstrip('/')
                if not rel or any(x in ('.','..') for x in Path(rel).parts):return self._json(400,{'error':'مسار الملف غير صالح.'})
                p=(DATA/rel).resolve()
                if DATA not in p.parents or not p.is_file() or p.is_symlink():return self._json(404,{'error':'الملف غير موجود أو المسار غير مسموح.'})
                if rel.startswith('05_System_Backups/') or rel.startswith('06_Security_Blackbox/'):return self._json(403,{'error':'حذف النسخ الاحتياطية والسجل الأمني محظور من مدير الملفات التجريبي.'})
                with dbconn() as c:
                    used=c.execute('SELECT id FROM products WHERE image_path=? OR video_path=? LIMIT 1',(rel,rel)).fetchone()
                    if used:return self._json(409,{'error':'الملف مرتبط بمعدة؛ أزل ارتباطه من ملف المعدة أولًا.'})
                    p.unlink();audit(c,s['user']['id'],'MEDIA_DELETED','file',rel,'admin file manager')
                return self._json(200,{'ok':True})
            if path == '/api/admin/ads':
                s=self._require_admin()
                if not s:return
                d=self._body_json(); key=str(d.get('slot_key','')); title=str(d.get('title','')).strip(); sub=str(d.get('subtitle','')).strip(); active=1 if d.get('active',True) else 0; target=str(d.get('target_url','')).strip()
                if target and not (target.startswith('/') or target.startswith('https://')):return self._json(400,{'error':'الرابط يجب أن يكون محليًا أو يبدأ بـ HTTPS.'})
                with dbconn() as c:
                    c.execute('UPDATE ad_slots SET title=?,subtitle=?,target_url=?,active=?,updated_at=? WHERE slot_key=?',(title,sub,target,active,stamp(),key)); audit(c,s['user']['id'],'AD_SLOT_UPDATED','ad_slot',key,title)
                return self._json(200,{'ok':True})
            return self._json(404,{'error':'المسار غير موجود.'})
        except ValueError as e: return self._json(400,{'error':str(e)})
        except sqlite3.IntegrityError as e: return self._json(409,{'error':'تعارض في البيانات أو القسم مرتبط بمحتويات أخرى.','detail':str(e) if os.environ.get('NAJEEB_DEBUG')=='1' else ''})
        except Exception as e: return self._json(500,{'error':'حدث خطأ داخلي.','detail':str(e) if os.environ.get('NAJEEB_DEBUG')=='1' else ''})
    def do_DELETE(self):
        path=urlparse(self.path).path
        try:
            if path.startswith('/api/admin/products/') and not path.endswith('/'):
                s=self._require_admin()
                if not s:return
                pid=int(path.rstrip('/').split('/')[-1])
                with dbconn() as c:
                    p=c.execute('SELECT title_ar FROM products WHERE id=?',(pid,)).fetchone()
                    if not p:return self._json(404,{'error':'المعدة غير موجودة.'})
                    active=c.execute("SELECT id FROM reservations WHERE product_id=? AND status='ACTIVE' AND expires_at>? LIMIT 1",(pid,stamp())).fetchone()
                    if active:return self._json(409,{'error':'لا يمكن أرشفة معدة لها حجز نشط.'})
                    c.execute('UPDATE products SET active=0,updated_at=? WHERE id=?',(stamp(),pid));audit(c,s['user']['id'],'PRODUCT_ARCHIVED','product',pid,p['title_ar'])
                return self._json(200,{'ok':True})
            if path.startswith('/api/admin/categories/'):
                s=self._require_admin()
                if not s:return
                cid=int(path.rstrip('/').split('/')[-1])
                with dbconn() as c:
                    if c.execute('SELECT 1 FROM categories WHERE parent_id=? LIMIT 1',(cid,)).fetchone() or c.execute('SELECT 1 FROM products WHERE category_id=? LIMIT 1',(cid,)).fetchone():return self._json(409,{'error':'لا يمكن حذف قسم يحتوي أقسامًا فرعية أو معدات.'})
                    c.execute('DELETE FROM categories WHERE id=?',(cid,)); audit(c,s['user']['id'],'CATEGORY_DELETED','category',cid,'admin action')
                return self._json(200,{'ok':True})
            return self._json(404,{'error':'المسار غير موجود.'})
        except Exception as e:return self._json(500,{'error':'تعذر تنفيذ الحذف.'})
    def _static(self, path, ctype=None, headers=None):
        path=Path(path).resolve()
        allowed=[STATIC.resolve(),ADMIN.resolve(),MERCHANT.resolve()]
        if not any(path==root or root in path.parents for root in allowed):return self._json(403,{'error':'forbidden'})
        if not path.is_file():return self._json(404,{'error':'الملف غير موجود.'})
        data=path.read_bytes(); typ=ctype or mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
        if typ.startswith('text/html') or typ in ('application/javascript','application/manifest+json'): pass
        if path.suffix=='.js':typ='application/javascript; charset=utf-8'
        if path.suffix=='.css':typ='text/css; charset=utf-8'
        if path.suffix=='.svg':typ='image/svg+xml'
        return self._send(200,data,typ,headers)
    def _media(self, rel):
        raw=unquote(rel).replace('\\','/').lstrip('/')
        relpath=Path(raw)
        if relpath.is_absolute() or any(x in ('.','..') for x in relpath.parts) or not raw:return self._json(404,{'error':'الملف غير موجود.'})
        root=DATA.resolve(); p=(root/relpath).resolve()
        if root not in p.parents or not p.is_file():return self._json(404,{'error':'الملف غير موجود.'})
        allowed=False
        with dbconn() as c:
            if relpath.parts[0] in ('03_Products_Images','02_Equipment_Videos'):
                allowed=bool(c.execute('SELECT 1 FROM products WHERE active=1 AND (image_path=? OR video_path=?) LIMIT 1',(raw,raw)).fetchone())
            elif len(relpath.parts)>=3 and relpath.parts[0]=='04_Spare_Parts' and relpath.parts[1]=='Approved':
                allowed=bool(c.execute("SELECT 1 FROM spare_parts p JOIN merchant_stores m ON m.id=p.store_id WHERE p.status='PUBLISHED' AND m.status='APPROVED' AND (p.image_path=? OR p.video_path=?) LIMIT 1",(raw,raw)).fetchone())
        if not allowed:return self._json(404,{'error':'الملف غير منشور أو غير معتمد.'})
        typ=mimetypes.guess_type(str(p))[0] or 'application/octet-stream'; size=p.stat().st_size
        range_header=self.headers.get('Range','')
        match=re.fullmatch(r'bytes=(\d*)-(\d*)',range_header.strip()) if range_header else None
        if match:
            start=int(match.group(1) or 0); end=int(match.group(2) or size-1)
            if start>=size or end<start:
                self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.send_header('Content-Length','0');self.end_headers();return
            end=min(end,size-1); length=end-start+1
            with open(p,'rb') as fh: fh.seek(start); data=fh.read(length)
            self.send_response(206);self.send_header('Content-Type',typ);self.send_header('Content-Length',str(len(data)));self.send_header('Content-Range',f'bytes {start}-{end}/{size}');self.send_header('Accept-Ranges','bytes');self.send_header('Cache-Control','public, max-age=86400');self.send_header('X-Content-Type-Options','nosniff');self.end_headers()
            try:self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):pass
            return
        self.send_response(200);self.send_header('Content-Type',typ);self.send_header('Content-Length',str(size));self.send_header('Accept-Ranges','bytes');self.send_header('Cache-Control','public, max-age=86400');self.send_header('X-Content-Type-Options','nosniff');self.end_headers()
        try:
            with open(p,'rb') as fh:
                while True:
                    chunk=fh.read(1024*1024)
                    if not chunk:break
                    self.wfile.write(chunk)
        except (BrokenPipeError,ConnectionResetError):pass
        return

def daily_backup_worker():
    """Run a daily encrypted backup after the configured local hour, only if a key exists."""
    enabled=os.environ.get('NAJEEB_AUTO_BACKUP','1').lower() not in ('0','false','no','off')
    if not enabled: return
    try:
        from backup_engine import key_bytes_from_env, aesgcm_class
        key_bytes_from_env(); aesgcm_class()
    except Exception as e:
        print(f'[backup] Daily backup is NOT active: {e}')
        return
    try: target_hour=max(0,min(23,int(os.environ.get('NAJEEB_BACKUP_HOUR','2'))))
    except ValueError: target_hour=2
    print(f'[backup] Daily encrypted backup enabled; local run hour={target_hour:02d}:00; output={BACKUP_ROOT}')
    while True:
        try:
            now_local=datetime.now().astimezone()
            if now_local.hour >= target_hour:
                state=backup_status(BACKUP_ROOT); latest=state.get('latest')
                last_local=datetime.fromtimestamp(Path(state['root'],latest['file']).stat().st_mtime).astimezone() if latest else None
                if not last_local or last_local.date()!=now_local.date():
                    if BACKUP_LOCK.acquire(blocking=False):
                        try:
                            result=create_backup(DB,DATA,BACKUP_ROOT,retain_days=int(os.environ.get('NAJEEB_BACKUP_RETAIN_DAYS','90')))
                            try: result['cloud_delivery']=upload_s3(Path(result['path']))
                            except Exception as cloud_err: result['cloud_delivery']={'configured':True,'uploaded':False,'reason':str(cloud_err)[:300]}
                            try: result['notification']=notify_backup(result,result.get('cloud_delivery'))
                            except Exception as notify_err: result['notification']={'sent':False,'reason':str(notify_err)[:300]}
                            print(f"[backup] Created {result['file']} sha256={result['sha256']} cloud={result.get('cloud_delivery',{}).get('uploaded',False)}")
                        except Exception as e: print(f'[backup] FAILED: {e}')
                        finally: BACKUP_LOCK.release()
        except Exception as e: print(f'[backup] Scheduler check failed: {e}')
        time.sleep(60)

def main():
    init_db()
    print('Al-Najeeb Ecosystem Pilot RC3')
    print(f'Client: http://{HOST}:{PORT}/')
    print(f'Admin:  http://{HOST}:{PORT}/admin/')
    print(f'Database: {DB}')
    print(f'Media root: {DATA}')
    print(f'Backup root: {BACKUP_ROOT}')
    print('Demo mode only: no payment gateway, SMS OTP, biometric verification, AI provider, or remote TLS is configured.')
    threading.Thread(target=daily_backup_worker,name='najeeb-daily-backup',daemon=True).start()
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    try: server.serve_forever()
    except KeyboardInterrupt: print('\nStopping local pilot...')
    finally: server.server_close()
if __name__=='__main__': main()

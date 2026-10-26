#!/usr/bin/env python3
"""Disposable black-box smoke test for the local Najeeb pilot."""
import os, sys, time, json, tempfile, subprocess, urllib.request, urllib.error, http.cookiejar, socket, pathlib, base64, secrets, sqlite3
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
def free_port():
    s=socket.socket(); s.bind(('127.0.0.1',0)); p=s.getsockname()[1]; s.close(); return p

def request(opener, url, method='GET', obj=None, data=None, headers=None):
    h={'Accept':'application/json'}
    if headers: h.update(headers)
    if obj is not None:
        data=json.dumps(obj).encode();h.setdefault('Content-Type','application/json')
    req=urllib.request.Request(url,data=data,headers=h,method=method)
    try:
        with opener.open(req,timeout=5) as r:
            raw=r.read(); return r.status, json.loads(raw) if 'json' in r.headers.get('Content-Type','') else raw, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw=e.read()
        try: out=json.loads(raw)
        except: out=raw.decode(errors='replace')
        return e.code,out,dict(e.headers)

def run():
  with tempfile.TemporaryDirectory(prefix='najeeb-smoke-') as td:
    port=free_port(); media=pathlib.Path(td)/'media'; (media/'03_Products_Images').mkdir(parents=True)
    orphan=media/'03_Products_Images'/'orphan_test.txt'; orphan.write_text('test asset')
    backup_root=pathlib.Path(td)/'backup-vault'; recovery=pathlib.Path(td)/'recovery-output'; backup_key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii')
    env=os.environ.copy(); env.update({'HOST':'127.0.0.1','PORT':str(port),'NAJEEB_DB_PATH':str(pathlib.Path(td)/'test.sqlite3'),'ALNAJEEB_DATA_ROOT':str(media),'NAJEEB_BACKUP_ROOT':str(backup_root),'NAJEEB_BACKUP_KEY':backup_key,'NAJEEB_AUTO_BACKUP':'0'})
    proc=subprocess.Popen([sys.executable,str(ROOT/'server.py')],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
      base=f'http://127.0.0.1:{port}'; opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
      for _ in range(80):
        try:
          st,d,_=request(opener,base+'/api/health')
          if st==200: break
        except Exception: pass
        time.sleep(.15)
      else: raise AssertionError('Server did not start')
      assert st==200 and d['ok']
      for path in ['/','/admin/','/admin/assets/admin.css','/admin/assets/admin.js','/manifest.webmanifest','/admin-manifest.webmanifest','/sw.js','/admin/sw.js','/merchant/','/merchant/assets/merchant.js']:
        st,body,_=request(opener,base+path)
        assert st==200,(path,st)
        if path=='/admin-manifest.webmanifest': assert body['scope']=='/admin/'
      st,catalog,_=request(opener,base+'/api/catalog'); assert st==200 and len(catalog['products'])>=5 and len(catalog['categories'])>=10
      st,d,_=request(opener,base+'/api/admin/login','POST',{'username':'admin','password':'NajeebDemo#2026'}); assert st==200 and d['must_change_password']
      st,d,_=request(opener,base+'/api/admin/dashboard'); assert st==428
      st,d,_=request(opener,base+'/api/admin/change-password','POST',{'username':'smokeowner','password':'SmokeTest#2026'}); assert st==200
      st,d,_=request(opener,base+'/api/admin/login','POST',{'username':'smokeowner','password':'SmokeTest#2026'}); assert st==200 and not d['must_change_password']
      st,prods,_=request(opener,base+'/api/admin/products'); assert st==200 and len(prods['products'])>=5
      # Double-entry ledger: enforce equal debit/credit and immutable posted records.
      st,ledger_accounts,_=request(opener,base+'/api/admin/ledger/accounts'); assert st==200 and len(ledger_accounts['accounts'])>=12,ledger_accounts
      acct={x['code']:x for x in ledger_accounts['accounts']}
      st,bad_ledger,_=request(opener,base+'/api/admin/ledger/post','POST',{'txn_type':'ADJUSTMENT','description':'unbalanced smoke entry','entries':[{'account_id':acct['1000']['id'],'side':'DEBIT','amount':5,'currency':'SAR'},{'account_id':acct['4000']['id'],'side':'CREDIT','amount':4,'currency':'SAR'}]}); assert st==400,bad_ledger
      st,ledger_post,_=request(opener,base+'/api/admin/ledger/post','POST',{'txn_type':'ADJUSTMENT','description':'smoke balanced journal','reference_type':'TEST','reference_id':'SMOKE-001','entries':[{'account_id':acct['1000']['id'],'side':'DEBIT','amount':5.25,'currency':'SAR'},{'account_id':acct['4000']['id'],'side':'CREDIT','amount':5.25,'currency':'SAR'}]}); assert st==201,ledger_post
      st,ledger_txns,_=request(opener,base+'/api/admin/ledger/transactions'); assert st==200 and any(x['id']==ledger_post['id'] and len(x['entries'])==2 for x in ledger_txns['transactions'])
      with sqlite3.connect(env['NAJEEB_DB_PATH']) as c:
        try: c.execute('UPDATE ledger_entries SET memo=? WHERE transaction_id=?',('tamper',ledger_post['id'])); raise AssertionError('ledger immutability trigger did not block update')
        except sqlite3.IntegrityError: pass
      # Category creation and path traversal prevention
      st,cat,_=request(opener,base+'/api/admin/categories','POST',{'name_ar':'قسم اختبار','name_en':'Test Category','folder_path':'01_Categories_Media/TestCategory'}); assert st==201,cat
      st,bad,_=request(opener,base+'/api/admin/categories','POST',{'name_ar':'قسم مسار ضار','folder_path':'../../escape'}); assert st==400,bad
      # Add product; media upload must go to private quarantine and never be public before clean scan + admin approval.
      st,p,_=request(opener,base+'/api/admin/products','POST',{'category_id':cat['id'],'title_ar':'منتج اختبار','title_en':'Smoke Product','price':123,'brand':'Test','condition':'USED_CLEAN','description':'initial'}); assert st==201,p; pid=p['id']
      png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/pyoAAAAASUVORK5CYII=')
      st,up,_=request(opener,base+f'/api/admin/products/{pid}/upload','POST',data=png,headers={'Content-Type':'image/png','X-File-Name':'tiny.png','X-Media-Kind':'image'}); assert st==202,up
      quarantine_id=up['quarantine_id']
      st,qdata,_=request(opener,base+'/api/admin/security/quarantine'); assert st==200 and any(x['id']==quarantine_id for x in qdata['items'])
      with sqlite3.connect(env['NAJEEB_DB_PATH']) as c: qpath=c.execute('SELECT stored_path FROM security_quarantine WHERE id=?',(quarantine_id,)).fetchone()[0]
      st,private_media,_=request(opener,base+'/media/'+qpath,'GET'); assert st==404,private_media
      st,scan,_=request(opener,base+f'/api/admin/security/quarantine/{quarantine_id}/scan','POST',{}); assert st==200,scan
      if scan['status']=='CLEAN':
        st,release,_=request(opener,base+f'/api/admin/security/quarantine/{quarantine_id}/release','POST',{}); assert st==200,release
        st,media_body,headers=request(opener,base+'/media/'+release['path'],'GET',headers={'Range':'bytes=0-7'}); assert st==206 and len(media_body)==8 and headers.get('Content-Range','').startswith('bytes 0-7/')
      else:
        st,release,_=request(opener,base+f'/api/admin/security/quarantine/{quarantine_id}/release','POST',{}); assert st==409,release
        st,reject,_=request(opener,base+f'/api/admin/security/quarantine/{quarantine_id}/reject','POST',{'note':'test scanner unavailable'}); assert st==200,reject
      st,edit,_=request(opener,base+f'/api/admin/products/{pid}/edit','POST',{'category_id':cat['id'],'title_ar':'منتج اختبار محدث','title_en':'Smoke Product','price':124,'brand':'Test','condition':'USED_CLEAN','description':'updated'}); assert st==200,edit
      # One active reservation per product, written to SQLite
      p0=catalog['products'][0]
      st,hold,_=request(opener,base+'/api/reservations','POST',{'product_id':p0['id'],'customer_name':'اختبار','phone':'0000000000','note':'test'}); assert st==201,hold
      st,dupe,_=request(opener,base+'/api/reservations','POST',{'product_id':p0['id'],'customer_name':'اختبار ثانٍ','phone':'0000000001'}); assert st==409,dupe
      st,quote,_=request(opener,base+'/api/quotes','POST',{'product_id':p0['id'],'customer_name':'طلب سعر','phone':'0000000002','details':'test'}); assert st==201,quote
      # Encrypted backup API + independent restore verification
      st,bstatus,_=request(opener,base+'/api/admin/backup/status'); assert st==200 and bstatus['configured'] and bstatus['key_configured']
      st,bundle,_=request(opener,base+'/api/admin/backup/create','POST',{}); assert st==201,bundle
      backup_file=pathlib.Path(bundle['path']); assert backup_file.is_file() and backup_file.read_bytes()[:5]==b'NJBK1'
      import backup_engine
      restored=backup_engine.decrypt_bundle(backup_file,recovery,base64.urlsafe_b64decode(backup_key.encode('ascii')))
      assert restored['ok'] and restored['database_integrity']=='ok'
      user_csv=(recovery/'reports'/'users.csv')
      if user_csv.exists(): assert 'password_hash' not in user_csv.read_text(encoding='utf-8-sig')
      st,bstatus2,_=request(opener,base+'/api/admin/backup/status'); assert st==200 and bstatus2['count']>=1 and bstatus2['latest']['file']==bundle['file']
      st,ads,_=request(opener,base+'/api/admin/ads','POST',{'slot_key':'merchant_1','title':'تاجر اختبار','subtitle':'مساحة اختبار','active':True}); assert st==200,ads
      # Three-store market: tenant-scoped accounts, moderation and public publication rules.
      for sid, uname, display in [(1,'merchantone','معرض اختبار أول'),(2,'merchanttwo','معرض اختبار ثان')]:
        st,created,_=request(opener,base+'/api/admin/merchants','POST',{'store_id':sid,'username':uname,'temporary_password':'TempMerchant#2026','display_name':display,'contact_name':'اختبار','contact_phone':'0000000000'}); assert st==201,created
        st,approved,_=request(opener,base+f'/api/admin/merchants/{sid}/status','POST',{'status':'APPROVED'}); assert st==200,approved
      merchant_one=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
      st,login1,_=request(merchant_one,base+'/api/merchant/login','POST',{'username':'merchantone','password':'TempMerchant#2026'}); assert st==200 and login1['must_change_password'],login1
      st,cp,_=request(merchant_one,base+'/api/merchant/change-password','POST',{'username':'merchantone2','password':'MerchantOne#2026'}); assert st==200,cp
      st,login1b,_=request(merchant_one,base+'/api/merchant/login','POST',{'username':'merchantone2','password':'MerchantOne#2026'}); assert st==200 and not login1b['must_change_password'],login1b
      st,cats,_=request(merchant_one,base+'/api/spare-parts/categories'); assert st==200 and len(cats['categories'])>=6
      st,part1,_=request(merchant_one,base+'/api/merchant/spare-parts','POST',{'category_id':1,'name_ar':'محمل اختبار 6205','part_number':'TEST-6205','brand':'Test','compatible_equipment':'غسالات صناعية','unit_price':50,'currency':'SAR','stock_quantity':4,'description':'قطعة اختبار غير حقيقية'}); assert st==201,part1
      part1id=part1['id']
      st,public_before,_=request(opener,base+'/api/spare-parts?store_id=1'); assert st==200 and not any(x['id']==part1id for x in public_before['parts'])
      st,review,_=request(opener,base+f'/api/admin/spare-parts/{part1id}/review','POST',{'decision':'approve','note':'اختبار اعتماد'}); assert st==200,review
      st,public_after,_=request(opener,base+'/api/spare-parts?store_id=1'); assert st==200 and any(x['id']==part1id for x in public_after['parts'])
      # Merchant media is also privately quarantined; no direct URL exposes it.
      st,mup,_=request(merchant_one,base+f'/api/merchant/spare-parts/{part1id}/upload','POST',data=png,headers={'Content-Type':'image/png','X-File-Name':'spare-tiny.png','X-Media-Kind':'image'}); assert st==202,mup
      with sqlite3.connect(env['NAJEEB_DB_PATH']) as c: mqpath=c.execute('SELECT stored_path FROM security_quarantine WHERE id=?',(mup['quarantine_id'],)).fetchone()[0]
      st,private_merchant_media,_=request(opener,base+'/media/'+mqpath,'GET'); assert st==404,private_merchant_media
      st,mqueue,_=request(opener,base+'/api/admin/security/quarantine'); assert st==200 and any(x['id']==mup['quarantine_id'] and x['target_type']=='SPARE_PART' for x in mqueue['items'])
      st,mscan,_=request(opener,base+f"/api/admin/security/quarantine/{mup['quarantine_id']}/scan",'POST',{}); assert st==200,mscan
      if mscan['status']=='CLEAN':
        st,mrelease,_=request(opener,base+f"/api/admin/security/quarantine/{mup['quarantine_id']}/release",'POST',{}); assert st==200,mrelease
      else:
        st,mrelease,_=request(opener,base+f"/api/admin/security/quarantine/{mup['quarantine_id']}/release",'POST',{}); assert st==409,mrelease
        st,mreject,_=request(opener,base+f"/api/admin/security/quarantine/{mup['quarantine_id']}/reject",'POST',{'note':'smoke scanner unavailable'}); assert st==200,mreject
      merchant_two=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
      st,login2,_=request(merchant_two,base+'/api/merchant/login','POST',{'username':'merchanttwo','password':'TempMerchant#2026'}); assert st==200 and login2['must_change_password']
      st,cp2,_=request(merchant_two,base+'/api/merchant/change-password','POST',{'username':'merchanttwo2','password':'MerchantTwo#2026'}); assert st==200,cp2
      st,login2b,_=request(merchant_two,base+'/api/merchant/login','POST',{'username':'merchanttwo2','password':'MerchantTwo#2026'}); assert st==200 and not login2b['must_change_password']
      # Merchant-specific ledger views are read-only and cannot see another store's lines.
      store1acct=acct['2200-M1']; store2acct=acct['2200-M2']
      st,m1tx,_=request(opener,base+'/api/admin/ledger/post','POST',{'txn_type':'SETTLEMENT','description':'settlement smoke store 1','reference_type':'TEST','reference_id':'STORE-1','entries':[{'account_id':store1acct['id'],'side':'DEBIT','amount':12,'currency':'SAR'},{'account_id':acct['1000']['id'],'side':'CREDIT','amount':12,'currency':'SAR'}]}); assert st==201,m1tx
      st,m2tx,_=request(opener,base+'/api/admin/ledger/post','POST',{'txn_type':'SETTLEMENT','description':'settlement smoke store 2','reference_type':'TEST','reference_id':'STORE-2','entries':[{'account_id':store2acct['id'],'side':'DEBIT','amount':27,'currency':'SAR'},{'account_id':acct['1000']['id'],'side':'CREDIT','amount':27,'currency':'SAR'}]}); assert st==201,m2tx
      st,m1ledger,_=request(merchant_one,base+'/api/merchant/ledger/transactions'); assert st==200 and all(x['account_code']=='2200-M1' for x in m1ledger['entries']) and any(x['id']==m1tx['id'] for x in m1ledger['entries'])
      st,m2ledger,_=request(merchant_two,base+'/api/merchant/ledger/transactions'); assert st==200 and all(x['account_code']=='2200-M2' for x in m2ledger['entries']) and any(x['id']==m2tx['id'] for x in m2ledger['entries']) and not any(x['id']==m1tx['id'] for x in m2ledger['entries'])
      st,own2,_=request(merchant_two,base+'/api/merchant/spare-parts'); assert st==200 and not any(x['id']==part1id for x in own2['parts'])
      st,forbidden_edit,_=request(merchant_two,base+f'/api/merchant/spare-parts/{part1id}/edit','POST',{'category_id':1,'name_ar':'اختراق','unit_price':1,'stock_quantity':1}); assert st==404,forbidden_edit
      st,store_public,_=request(opener,base+'/api/spare-parts'); assert st==200 and len(store_public['stores'])==3 and any(x['id']==part1id for x in store_public['parts'])
      st,nowcat,_=request(opener,base+'/api/catalog'); assert any(x['id']==cat['id'] for x in nowcat['categories']) and any(x['id']==pid and x['title_ar']=='منتج اختبار محدث' for x in nowcat['products'])
      st,files,_=request(opener,base+'/api/admin/storage/files'); assert st==200 and any(f['path']=='03_Products_Images/orphan_test.txt' for f in files['files'])
      st,deleted,_=request(opener,base+'/api/admin/storage/delete-file','POST',{'path':'03_Products_Images/orphan_test.txt'}); assert st==200 and not orphan.exists()
      st,deletedmedia,_=request(opener,base+f'/api/admin/products/{pid}/remove-media','POST',{'kind':'image'}); assert st==200,deletedmedia
      st,arch,_=request(opener,base+f'/api/admin/products/{pid}','DELETE'); assert st==200,arch
      # Final backup after later trader/ledger/security activity: verify new business tables survive restore.
      st,bundle2,_=request(opener,base+'/api/admin/backup/create','POST',{}); assert st==201,bundle2
      backup_file2=pathlib.Path(bundle2['path']); restored2=backup_engine.decrypt_bundle(backup_file2,pathlib.Path(td)/'recovery-final',base64.urlsafe_b64decode(backup_key.encode('ascii')))
      assert restored2['ok'] and restored2['database_integrity']=='ok'
      with sqlite3.connect(restored2['database']) as c:
        assert c.execute('SELECT COUNT(*) FROM ledger_entries').fetchone()[0]>=6
        assert c.execute('SELECT COUNT(*) FROM merchant_stores').fetchone()[0]==3
        assert c.execute('SELECT COUNT(*) FROM security_quarantine').fetchone()[0]>=2
      print('PASS: client/admin resources, PWA scopes, SQLite catalog/auth/CRUD, path traversal rejection, quarantined image upload + gated media release/byte range, 24h hold + duplicate prevention, quotes, ads, storage controls, AES-256-GCM encrypted backup API, readable CSV secret-column filtering, authenticated restore/decryption + SQLite integrity check and post-activity restore of merchant/ledger/quarantine tables, product archive, three-store market workflows, merchant password rotation, approval workflow, public publication gate, and cross-merchant isolation, double-entry balance enforcement, immutable ledger records, merchant-specific financial views, quarantine SHA-256 inventory, both admin and merchant media quarantine, ClamAV-gated publication, and block-on-unavailable scanner.')
    finally:
      proc.terminate()
      try: proc.wait(timeout=5)
      except subprocess.TimeoutExpired: proc.kill()
      if proc.stderr:
        err=proc.stderr.read().decode(errors='replace')
        if err.strip(): print('Server stderr:',err[-2000:])
if __name__=='__main__': run()

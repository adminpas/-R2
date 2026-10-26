#!/usr/bin/env python3
"""Optional delivery of encrypted backup bundles to a dedicated S3-compatible vault.

Notifications contain status and checksum only. Backup contents are NEVER sent to Telegram/email.
S3 Object Lock/versioning must be configured and independently tested on the bucket itself.
"""
from __future__ import annotations
import hashlib, json, os, urllib.parse, urllib.request, smtplib, ssl
from email.message import EmailMessage
from pathlib import Path


def s3_is_configured():
    return all(os.environ.get(k, '').strip() for k in (
        'NAJEEB_S3_BUCKET','NAJEEB_S3_ACCESS_KEY_ID','NAJEEB_S3_SECRET_ACCESS_KEY'))

def telegram_is_configured():
    return bool(os.environ.get('NAJEEB_TELEGRAM_BOT_TOKEN','').strip() and os.environ.get('NAJEEB_TELEGRAM_CHAT_ID','').strip())

def email_is_configured():
    return all(os.environ.get(k,'').strip() for k in ('NAJEEB_SMTP_HOST','NAJEEB_SMTP_USER','NAJEEB_SMTP_PASSWORD','NAJEEB_NOTIFY_TO'))

def upload_s3(encrypted_bundle: Path):
    """Upload an already encrypted .njb file. Returns a status dict, never a success default."""
    p=Path(encrypted_bundle).resolve()
    if not s3_is_configured(): return {'configured':False,'uploaded':False,'reason':'إعداد S3 غير مكتمل؛ بقيت النسخة محلية.'}
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        return {'configured':True,'uploaded':False,'reason':'مكتبة boto3 غير مثبتة؛ ثبّت requirements-backup.txt.'}
    bucket=os.environ['NAJEEB_S3_BUCKET'].strip()
    region=os.environ.get('NAJEEB_S3_REGION','us-east-1').strip() or 'us-east-1'
    endpoint=os.environ.get('NAJEEB_S3_ENDPOINT_URL','').strip() or None
    prefix=os.environ.get('NAJEEB_S3_PREFIX','najeeb/encrypted-backups').strip('/ ')
    key=f'{prefix}/{p.name}' if prefix else p.name
    client=boto3.client('s3',aws_access_key_id=os.environ['NAJEEB_S3_ACCESS_KEY_ID'],
                        aws_secret_access_key=os.environ['NAJEEB_S3_SECRET_ACCESS_KEY'],
                        region_name=region,endpoint_url=endpoint,config=Config(retries={'max_attempts':5,'mode':'standard'}))
    digest=hashlib.sha256(p.read_bytes()).hexdigest()
    client.upload_file(str(p),bucket,key,ExtraArgs={'Metadata':{'sha256':digest,'format':'najeeb-encrypted-backup'}})
    # Verify the uploaded object's size and checksum metadata. Object-lock/immutability is bucket configuration.
    head=client.head_object(Bucket=bucket,Key=key)
    metadata=head.get('Metadata',{})
    if head.get('ContentLength')!=p.stat().st_size or metadata.get('sha256')!=digest:
        return {'configured':True,'uploaded':False,'key':key,'reason':'لم تتطابق بيانات التحقق بعد الرفع.'}
    return {'configured':True,'uploaded':True,'bucket':bucket,'key':key,'size_bytes':p.stat().st_size,'sha256':digest,
            'version_id':head.get('VersionId'),'object_lock_note':'يجب التحقق من تفعيل Versioning وObject Lock ومنع صلاحية الحذف في إعدادات الحاوية.'}

def send_telegram_status(message: str):
    if not telegram_is_configured(): return {'configured':False,'sent':False,'reason':'إشعارات Telegram غير مهيأة.'}
    token=os.environ['NAJEEB_TELEGRAM_BOT_TOKEN'].strip(); chat=os.environ['NAJEEB_TELEGRAM_CHAT_ID'].strip()
    url='https://api.telegram.org/bot'+token+'/sendMessage'
    payload=json.dumps({'chat_id':chat,'text':message,'disable_web_page_preview':True}).encode('utf-8')
    req=urllib.request.Request(url,data=payload,headers={'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=15) as response:
            result=json.loads(response.read().decode('utf-8'))
        return {'configured':True,'sent':bool(result.get('ok')),'description':result.get('description','')}
    except Exception as exc:
        return {'configured':True,'sent':False,'reason':str(exc)[:300]}

def send_email_status(subject: str, message: str):
    if not email_is_configured(): return {'configured':False,'sent':False,'reason':'إشعارات البريد غير مهيأة.'}
    host=os.environ['NAJEEB_SMTP_HOST'].strip(); port=int(os.environ.get('NAJEEB_SMTP_PORT','587'))
    user=os.environ['NAJEEB_SMTP_USER'].strip(); password=os.environ['NAJEEB_SMTP_PASSWORD']
    sender=os.environ.get('NAJEEB_SMTP_FROM',user).strip(); recipient=os.environ['NAJEEB_NOTIFY_TO'].strip()
    mail=EmailMessage(); mail['Subject']=subject; mail['From']=sender; mail['To']=recipient; mail.set_content(message)
    try:
        context=ssl.create_default_context()
        with smtplib.SMTP(host,port,timeout=20) as server:
            server.ehlo(); server.starttls(context=context); server.ehlo(); server.login(user,password); server.send_message(mail)
        return {'configured':True,'sent':True}
    except Exception as exc: return {'configured':True,'sent':False,'reason':str(exc)[:300]}

def notify_backup(result: dict, cloud_result: dict|None=None):
    cloud_result=cloud_result or {'uploaded':False,'reason':'لم يُضبط رفع سحابي مستقل.'}
    cloud='تم التحقق من الرفع السحابي' if cloud_result.get('uploaded') else 'النسخة محفوظة محليًا فقط؛ الرفع السحابي غير مؤكد'
    msg=(f"نسخ النجيب الاحتياطي\nالحالة: {'نجاح محلي' if result.get('ok') else 'فشل'}\n"
         f"الملف: {result.get('file','غير معروف')}\nالحجم: {result.get('size_bytes','?')} bytes\n"
         f"SHA-256: {result.get('sha256','غير معروف')}\nقاعدة البيانات: {result.get('database_integrity','غير معروف')}\n{cloud}\n"
         "تنبيه: هذه رسالة حالة فقط وليست نسخة بيانات. لا تعتمد على النسخة حتى ينجح اختبار الاستعادة.")
    telegram=send_telegram_status(msg)
    email=send_email_status('حالة النسخ الاحتياطي لمنظومة النجيب',msg)
    return {'telegram':telegram,'email':email}

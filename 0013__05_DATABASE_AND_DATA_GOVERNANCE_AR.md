# قاعدة البيانات وحوكمة البيانات

## 1. وصف الحالة الحالية

قاعدة المعاينة RC4 هي SQLite، وبها جداول `users`, `categories`, `products`, `reservations`, `quote_requests`, `ad_slots`, `audit_log`, `merchant_stores`, `spare_part_categories`, `spare_parts`, `security_quarantine`, `financial_accounts`, `ledger_transactions`, `ledger_entries`, و`spare_part_quote_requests`. يحفظ المصدر مخططًا تجريبيًا للعمليات. المخطط التفصيلي دون البيانات موجود في `02_DATABASE/RC4_SQLITE_SCHEMA_ONLY.sql`.

**ملاحظة حرجة:** الأرشيفات التاريخية تتضمن مسار PostgreSQL/Drizzle أيضًا. SQLite الحالي ليس قاعدة الإنتاج المعتمدة تلقائيًا. يجب تثبيت قرار نهائي بنوع قاعدة البيانات ومكانها ومواعيد ترحيلها.

## 2. مجموعات البيانات المستهدفة

- الهوية والأمن: users, roles, permissions, sessions, device approvals, audit_logs, security_events, access_reviews.
- الكتالوج: categories, category_spec_schemas, brands, equipment_products, product_specs, product_conditions, product_documents, media_assets.
- سوق القطع: merchants, merchant_stores, spare_part_categories, spare_parts, stock_movements, merchant_reviews, merchant_agreements.
- البيع والطلب: customers, carts (إن لزم), quote_requests, orders, order_items, bookings/reservations, delivery_jobs, installation_jobs, maintenance_jobs.
- الأموال: payment_intents, payment_transactions, refunds, financial_accounts, journal_transactions, journal_entries, commissions, settlements, reconciliation_runs.
- الحماية: quarantine_items, scan_results, media_publications, malware_signatures/version, security_events.
- النسخ: backup_jobs, backup_artifacts, backup_checksums, restore_drills, storage_targets.
- التقارير: report_exports مع المالك والغرض والوقت والصلاحية والبصمة والانتهاء.
- تكاملات: notification_outbox, webhook_inbox, idempotency_keys, provider_events.
- صناعي/مستقبلي: equipment_passports, passport_events, iot_telemetry, ai_assessments, 3d_assets، إذا تم اعتمادها وتنفيذها.

## 3. قواعد البيانات

1. معرف ثابت لكل سجل وعدم إعادة استخدام المفاتيح.
2. استخدام `DECIMAL/NUMERIC` أو عدد صحيح بوحدة الهللة/القروش للمبالغ؛ لا تعتمد على `REAL/FLOAT` في دفتر الإنتاج.
3. قيد عملة صريح لكل مبلغ، وعدم جمع عملات مختلفة في رصيد واحد دون سعر صرف موثق.
4. Foreign Keys وقيود `CHECK` وفهارس مناسبة؛ لا حذف متسلسل لسجلات مالية أو تدقيق.
5. كل جدول متعدد التجار يتضمن `merchant_id/store_id` عند الحاجة، لكن التفويض لا يعتمد على معرف يرسله العميل فقط.
6. تسجل الأحداث المالية مرة واحدة عبر مفتاح idempotency ومصدر عملية واضح.
7. القيد المنشور غير قابل للتعديل أو الحذف؛ التصحيح بعكس/تسوية جديدة.
8. حذف المنتج يكون أرشفة/تعطيل عند ارتباطه بطلب/فاتورة/حجز.
9. سجل تدقيق يضم من/متى/ماذا/الكيان/النتيجة ومعرف الطلب؛ لا تسجل كلمات مرور أو رموزًا كاملة.
10. ترحيلات DB مرقمة وقابلة للمراجعة والرجوع، وتجرب على Staging مع نسخة قبل التطبيق.

## 4. قاموس بيانات أساسي

| الكيان | أهم الحقول | ضابط رئيسي |
|---|---|---|
| user | id, username, password_hash, role/status | كلمات مرور مجزأة، تغيير أول دخول، MFA للمدير |
| category | id, parent_id, name_ar/en, slug, sort_order, spec_schema | منع الدورات في الشجرة |
| equipment_product | id, category_id, SKU, brand/model, condition, status, currency, price | التحقق من مواصفات الفئة، السعر خادمي |
| merchant_store | id, owner_user_id, slug, status | اعتماد قبل النشر، مالك مرتبط بحساب محدد |
| spare_part | id, store_id, category_id, part_number, compatibility, stock | عزل متجر، مراجعة قبل النشر |
| media_asset | id, storage_key, checksum, content_type, scan_status, owner | مسار غير مباشر، يمنع النشر قبل الفحص |
| booking | id, product_id, customer_id, status, expires_at, deposit_state | قفل فريد للمنتج ومهلة خادمية |
| order/payment | id, currency, amount_minor, status, provider_ref | Webhook موثق ومنع التكرار |
| journal_transaction | id, txn_key, source, status, created_by | منشور غير قابل للمحو |
| journal_entry | txn_id, account_id, debit/credit, amount_minor, currency | مجموع المدين = مجموع الدائن لكل عملة |
| audit_log | actor, action, entity, outcome, timestamp | سجل إلحاقي ومحمّي |
| backup_artifact | id, range, checksum, key_ref, storage_target, state | التحقق من النسخة واختبار استعادتها |

## 5. قاعدة الأرقام المالية

يُحفظ المبلغ بوحدة صغيرة صحيحة (مثل هللة SAR) أو `NUMERIC(19,4)` حسب سياسة العملات والضرائب المعتمدة. لا يُستخدم float في دفتر إنتاج. تُحفظ عملة المعاملة الأصلية، ومبالغ الضريبة والأساس ونسبها في وقت الفاتورة، ونسخة من السعر والشروط المتفق عليها كي لا يغير تحديث المنتج مستندًا تاريخيًا.

## 6. الخصوصية والاحتفاظ

تعريف البيانات المصنفة: عامة، داخلية، سرية تجارية، شخصية، مالية/شديدة الحساسية، أسرار أمنية. تحديد غرض الجمع، أساس المعالجة، صلاحيات الوصول، مدة الاحتفاظ، وسيناريو الحذف/الإتلاف. قواعد الاستبقاء النهائية يحددها المستشار حسب النظام المنطبق والالتزامات المحاسبية، ولا يُفترض أن حذف كل البيانات تلقائيًا هو الحل.

## 7. الملفات الفنية

- `02_DATABASE/RC4_SQLITE_SCHEMA_ONLY.sql`: DDL مأخوذ من مخطط التشغيل الحالي دون سجلات بيانات.
- `02_DATABASE/LOGICAL_DATA_MODEL_AR.md`: تصور الكيانات والروابط المنطقية.
- `03_DIAGRAMS/ERD_LOGICAL_MODEL.mmd`: مخطط مرئي قابل للتحرير.

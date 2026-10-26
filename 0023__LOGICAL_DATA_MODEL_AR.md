# النموذج المنطقي لقاعدة البيانات — المقترح الإنتاجي

هذا وصف كيانات موجه للتصميم؛ لا يُعد migration جاهزًا. يجب تحويله إلى مخطط DDL ثم مراجعة القيود والفهارس والتعامل مع الحذف والمعاملات حسب محرك الإنتاج المختار.

## علاقات رئيسية

- `users` 1:N `audit_logs`, `sessions`, وأحداث الإجراءات.
- `categories` شجرة ذاتية parent-child؛ `products` ترتبط بفئة واحدة أساسية ومجموعة مواصفات موثقة.
- `merchant_stores` 1:N `spare_parts`, `stock_movements`, `quote_requests`, `merchant_agreements`, `merchant_reports`.
- `products` 1:N `bookings`, `order_items`, `maintenance_requests`, `media_assets` عبر روابط مخصصة.
- `orders` 1:N `order_items`, `payment_transactions`, `refunds`, `delivery_jobs`, `service_jobs`.
- `journal_transactions` 1:N `journal_entries`; يجب أن تتوازن السطور في عملية ترحيل ذرية.
- `media_assets` ترتبط بمعرف مالك/متجر ونوع المحتوى وحالة الفحص والبصمة ومفتاح التخزين.
- `backup_jobs` 1:N `backup_artifacts`; كل artifact له hash/إصدار/هدف/حالة ومراجع المفتاح، دون سر المفتاح نفسه.
- `security_events` تشير إلى مستخدم/ملف/جهاز/عملية مع معلومات كافية للتحقيق دون تسجيل أسرار.

## سياسة فصل المتاجر

أي جدول ببيانات تاجر يجب أن يحمل `merchant_id` أو يرتبط بكيان متجر معتمد. طبقة API تفرض نطاق متجر الجلسة، ويمكن استخدام PostgreSQL Row Level Security كطبقة إضافية بعد تصميم ومراجعة مناسبة، لا كبديل عن فحوص التطبيق.

## مفاتيح أساسية مقترحة

UUID/ULID لسجلات عامة أو IDs تسلسلية حسب متطلبات التنفيذ. لا تُفهم المعرفات على أنها أسرار. كل تنزيل/تعديل يحتاج authorization مستقل. الحقول الزمنية تحفظ UTC مع عرض محلي لـ Asia/Riyadh عند العرض في النظام السعودي؛ تُوثق سياسة التوقيت لأي تقارير محاسبية.

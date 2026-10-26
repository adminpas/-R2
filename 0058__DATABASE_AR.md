# ملف قاعدة البيانات

## المحرك
PostgreSQL عبر Drizzle ORM.

## مجموعات الجداول
- الكتالوج: products, categories, services, bundles.
- العملاء: customers, cart_items.
- التجارة: bookings, orders, payment_transactions, payment_settings.
- العمليات: maintenance_requests, technicians, special_requests.
- التسويق: ads, news_items, auctions, auction_bids, clearance_deals.
- الثقة: equipment_passport, passport_events.
- الأمن: audit_logs, security_events, blocklist.
- المراقبة الصناعية: iot_telemetry (قراءات مستشعرات مع حالة حدّية ووقت القياس والاستقبال).
- الوسائط والإعدادات: media_assets, site_settings.

## التكامل
العلاقات تستخدم Foreign Keys وحذفاً متدرجاً أو Set Null حسب طبيعة السجل. الأسعار والمبالغ أعداد صحيحة بالريال لتجنب أخطاء الكسور.

## قبل الإنتاج
- نسخ احتياطي يومي + Point-in-Time Recovery.
- فهرسة الحقول عالية الاستخدام (phone, status, category, reserved_until, created_at).
- سياسة احتفاظ وحذف متوافقة مع PDPL.
- بيئة Staging منفصلة عن Production.
- منع seed التوضيحي في قاعدة الإنتاج.


## شجرة التصنيفات والمواصفات الفنية

- المصدر المنظم: `src/lib/industryTaxonomy.ts`، ويحتوي الفروع المتخصصة والحقول الفنية وتعليمات خبير AI.
- آلية التهيئة: `ensureTaxonomy()` في `src/lib/taxonomy.ts` يضيف ما ينقص فقط. لا يستبدل بيانات القسم إذا سبق للمدير تعبئة مخطط المواصفات أو تعليمات AI.
- مفاتيح الأقسام الأصلية محفوظة لتفادي كسر المنتجات القائمة. يضاف التنظيف الجاف والمعدات المساعدة كقسمين رئيسيين جديدين.
- المنتج يحفظ `categoryPath` كمسار كامل، وتدمج المواصفات من الجذر حتى الفرع النهائي باستخدام `mergePathSpecSchemas()`.
- لا توجد منتجات أو أسعار فعلية تُنشأ من الدليل؛ بيانات المنتجات والوسائط والأسعار يجب إدخالها والتحقق منها في لوحة الإدارة.
- المخطط الحالي يخزن الإرشادات الفنية في `aiInstructions` والحقول القابلة للتعديل في `specSchema` دون الحاجة إلى عمود قاعدة بيانات جديد.


## IoT
- يسجل `iot_telemetry` القياسات التاريخية ولا يغيّر حالة المخزون أو ينفذ صيانة تلقائية.
- استقبال البيانات معطل إذا لم يُضبط `IOT_INGEST_TOKEN` بطول 32 حرفاً عشوائياً على الأقل.
- جداول القياس تُضاف عبر Drizzle على بيئة اختبار أولاً؛ لا تُعدَّل قاعدة الإنتاج دون نسخة احتياطية ومراجعة.

# Gold Quant Bot

بوت تداول كمي للذهب XAUUSD يعمل عبر Telegram، ويجمع بين تحليل السوق، إدارة دورة حياة الصفقة، التعلم الآلي، ومراجعة Gemini عند تفعيلها.

## بنية المشروع

- `bot.py` — التطبيق الرئيسي ومحركات السوق والصفقات والتعلم.
- `tests/` — اختبارات regression الأساسية.
- `requirements.txt` — اعتماديات التشغيل.
- `.gitignore` — يمنع تسريب ملفات التشغيل المحلية والأسرار والـbytecode.

لا توجد workflows مؤقتة أو patch runners داخل المستودع في النسخة النظيفة الحالية.

## بيانات السوق

Yahoo Finance هو المصدر الأساسي لبيانات XAUUSD والشموع التاريخية، مع استخدام `XAUUSD=X` للسعر الفوري و`DX-Y.NYB` و`^TNX` للمؤشرات المساندة. لا يستخدم مسار الإنتاج مصادر سعر ذهب بديلة مثل العقود الآجلة أو العملات المشفرة أو scraping.

## التشغيل

اضبط متغيرات البيئة المطلوبة: `TELEGRAM_TOKEN` و`BOT_PASSWORD` و`DATABASE_URL` عند استخدام PostgreSQL، و`GEMINI_API_KEY` عند تفعيل Gemini. ثم ثبّت الاعتماديات من `requirements.txt` وشغّل `python bot.py`.

## قاعدة البيانات

يفضل PostgreSQL عند توفير `DATABASE_URL`. SQLite متاح كخيار محلي/احتياطي للتشغيل التطويري.

## التحقق

الحد الأدنى قبل النشر:

`python3 -m py_compile bot.py`

`python3 tests/test_market_provider_hygiene.py`

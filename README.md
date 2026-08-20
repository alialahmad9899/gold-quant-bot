# Gold Quant Bot

بوت تداول كمي للذهب XAU/USD يعمل عبر Telegram، ويجمع بين التحليل متعدد الأطر، SMC/HMM/ML، إدارة دورة حياة الصفقة، Gemini، Trade Lawyer، وLive News Intelligence.

## بنية المشروع

- `bot.py` — التطبيق الرئيسي ومحركات السوق والصفقات والتعلم.
- `phase2_runtime_integration.py` — ربط الإشارة بدورة حياة الصفقة ومحامي الصفقة.
- `institutional_trade_review.py` — مراجعة مخاطر مرنة قبل الدخول.
- `signal_safety.py` — حواجز سلامة البيانات وتكرار الشمعة.
- `news_intelligence.py` — تجميع وتحليل الأخبار قرب اللحظة.
- `production_hardening.py` — معايرة Gemini، الأخبار، اتجاهات BUY/SELL، وobservability.
- `tests/` — اختبارات regression.

## بيانات السوق

Twelve Data هو مصدر XAU/USD الوحيد في الإنتاج. يتم فصل **Live Quote** عن **Historical Candles**؛ WebSocket يستخدم للسعر الحي عند توفره، وTime Series التاريخية تستخدم للتحليل M15/H1/H4. لا يوجد Yahoo Finance fallback للذهب.

## ذكاء الأخبار

توجد طبقة Live News Intelligence تجمع RSS/GDELT، تصنف أثر الخبر على الذهب، تلتقط Actual/Forecast/Previous عندما تكون منشورة، تجمع المقالات التي تمثل الحدث نفسه، وتربط الخبر بمراقبة حركة السعر قبل اعتبار الخبر سبباً لدخول فوري. الأخبار لا تتجاوز بوابات المخاطر أو تستبدل Twelve Data.

## الذكاء الاصطناعي المرن

Gemini في المراجعة المؤسسية مستشار adversarial وليس بوابة صارمة افتراضياً. الـhard veto محصور في المخاطر البنيوية/البيانات غير الصالحة وقواعد المخاطر الصريحة. يمكن تفعيل Gemini hard veto صراحة عبر `INSTITUTIONAL_AI_VETO=1` أو `SIGNAL_SAFETY_GEMINI_HARD_VETO=1`.

## Trade Lawyer

محامي الصفقة يعمل تلقائياً أثناء وجود صفقة نشطة ويقدم HOLD / PROTECT_PROFIT / REDUCE_RISK / ADD_ON_CONFIRMATION / EXIT / PREPARE_REVERSAL، مع أوامر Telegram `/lawyer` و`/news` والأزرار التفاعلية.

## التشغيل

اضبط متغيرات البيئة المطلوبة: `TELEGRAM_TOKEN` و`BOT_PASSWORD` و`DATABASE_URL` عند استخدام PostgreSQL، و`GEMINI_API_KEY` عند تفعيل Gemini، و`TWELVE_DATA_API_KEY`. ثم ثبّت الاعتماديات من `requirements.txt` وشغّل `python bot.py`.

## قاعدة البيانات

يفضل PostgreSQL عند توفير `DATABASE_URL`. SQLite متاح للتشغيل المحلي/التطويري. حالة اتجاهات BUY/SELL وملخصات أحداث الأخبار تحفظ في قاعدة البيانات عند توفرها.

## التحقق

الحد الأدنى قبل النشر:

`python3 -m py_compile bot.py`

`pytest -q`

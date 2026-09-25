from __future__ import annotations

import asyncio, json, logging, os, secrets, threading
from datetime import datetime, time as dtime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from radar_engine import RadarEngine
from storage import Storage

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("crypto_radar")

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PASSWORD = os.getenv("BOT_PASSWORD", "").strip()
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "8"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Amsterdam")
ALERT_SCORE = float(os.getenv("ALERT_SCORE_THRESHOLD", "84"))
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN_SECONDS", str(6 * 3600)))

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN غير مضبوط على Render.")
if not PASSWORD:
    raise RuntimeError("BOT_PASSWORD غير مضبوط على Render.")

engine = RadarEngine()
storage = Storage(os.getenv("DATABASE_URL"))
authorized: set[int] = set()
state = {"last_scan_at": None, "last_scan_count": 0, "last_error": None, "running": True}


def admin_ids() -> set[int]:
    return {ADMIN_CHAT_ID} if ADMIN_CHAT_ID else set()


def is_authorized(chat_id: int) -> bool:
    return chat_id in authorized or chat_id in admin_ids()


def keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔎 فحص الآن"), KeyboardButton("🟢 فرص شراء"), KeyboardButton("🏆 الأعلى")],
            [KeyboardButton("🌅 التقرير"), KeyboardButton("🧪 فحص الخدمات"), KeyboardButton("📊 الحالة")],
            [KeyboardButton("ℹ️ عن الرادار"), KeyboardButton("🚪 خروج")],
        ],
        resize_keyboard=True,
    )


async def deny(update: Update) -> None:
    await update.effective_message.reply_text(
        "🔐 أرسل /auth كلمة_المرور لفتح الرادار.", reply_markup=keyboard()
    )


def candidate_text(c: dict, rank: int | None = None) -> str:
    m = c.get("market", {})
    sm = c.get("smart_money", {})
    sec = c.get("security_status", "UNKNOWN")
    flags = ", ".join(c.get("risk_flags", [])[:4]) or "لا توجد"
    label = f"{rank}) " if rank else ""
    sm_line = (
        f"🧠 Smart Money: {sm.get('smart_money_count', 0)} محافظ | "
        f"صافي {sm.get('net_flow_usd', 0):,.0f}$"
        if sm.get("available") else "🧠 Smart Money: غير متاح حاليًا"
    )
    signal_map = {
        "BUY_WATCH": "🟢 فرصة شراء",
        "WATCH": "🟡 مراقبة",
        "AVOID": "🔴 تجنب",
    }
    signal = signal_map.get(c.get("signal"), "⚪ غير حاسم")
    return (
        f"{label}🪙 {c['symbol']} — {c['name']}\n"
        f"⛓️ {c['chain']} | القرار: {signal}\n"
        f"📊 الدرجة: {c['score']}/100 | أمان: {sec}\n"
        f"💵 السعر: {m.get('price_usd', 0):.10g}\n"
        f"💧 السيولة: {m.get('liquidity_usd', 0):,.0f}$ | MCap: {m.get('market_cap', 0):,.0f}$\n"
        f"📈 الحجم 1h: {m.get('volume_1h', 0):,.0f}$ | تسارع {m.get('volume_acceleration', 0):.1f}x\n"
        f"👥 نسبة المشترين 1h: {m.get('buyer_ratio_1h', 0) * 100:.1f}%\n"
        f"{sm_line}\n"
        f"📰 أخبار: {c.get('news', {}).get('mentions', 0)} إشارات\n"
        f"⚠️ مخاطر: {flags}\n"
        f"🔗 {c['url']}"
    )


async def run_scan() -> list[dict]:
    try:
        result = await asyncio.to_thread(engine.scan)
        state["last_scan_at"] = datetime.now(timezone.utc).isoformat()
        state["last_scan_count"] = len(result)
        state["last_error"] = None
        storage.save_candidates(result)
        logger.info(
            "scan ok: discovered=%s market_rows=%s candidates=%s providers=%s",
            getattr(engine, "last_scan", {}).get("discovered", 0),
            getattr(engine, "last_scan", {}).get("market_rows", 0),
            len(result),
            {k: v.get("state") for k, v in getattr(engine, "last_provider_status", {}).items()},
        )
        return result
    except Exception as exc:
        state["last_error"] = str(exc)
        logger.exception("scan failed")
        raise


async def send_alerts(app, candidates: list[dict]) -> None:
    recipients = sorted(set(authorized) | admin_ids())
    if not recipients:
        return
    for c in candidates:
        strong = (
            c["score"] >= ALERT_SCORE
            and c["security_status"] == "PASS"
            and c["data_completeness"] >= 0.75
            and c["smart_money"].get("available")
            and c["smart_money"].get("net_flow_usd", 0) > 0
        )
        if not strong or not storage.alert_allowed(c["token_key"], ALERT_COOLDOWN):
            continue
        msg = "🚨 تنبيه Crypto Radar — رصد قوي بالبيانات المتاحة، وليس توصية شراء\n\n" + candidate_text(c)
        for chat_id in recipients:
            try:
                await app.bot.send_message(chat_id, msg, disable_web_page_preview=True)
            except Exception:
                logger.exception("alert delivery failed")
        storage.record_alert(c)


def report_text(candidates: list[dict]) -> str:
    stats = storage.stats()
    lines = [
        "🌅 CRYPTO RADAR — التقرير الصباحي",
        f"وقت التقرير: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"المرشحون الحاليون: {len(candidates)} | المحفوظون: {stats['saved_candidates']}",
        "",
        "الدرجة ترتيب تحليلي متعدد العوامل وليست احتمالًا مضمونًا للصعود.",
        "",
    ]
    for i, c in enumerate(candidates[:8], 1):
        lines.append(
            f"{i}. {c['symbol']} | {c['chain']} | {c['score']}/100 | "
            f"أمان={c['security_status']} | SM={c['smart_money'].get('smart_money_count', 0)}"
        )
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await deny(update)
    authorized.add(update.effective_chat.id)
    await update.effective_message.reply_text(
        "✅ تم فتح Crypto Radar. استخدم /scan أو الأزرار.", reply_markup=keyboard()
    )


async def auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    supplied = " ".join(context.args).strip()
    if not supplied or not secrets.compare_digest(supplied, PASSWORD):
        return await update.effective_message.reply_text("❌ كلمة المرور غير صحيحة.")
    authorized.add(update.effective_chat.id)
    await update.effective_message.reply_text("✅ تم تسجيل الدخول.", reply_markup=keyboard())


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    authorized.discard(update.effective_chat.id)
    await update.effective_message.reply_text("🚪 تم الخروج.", reply_markup=keyboard())


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await deny(update)
    await update.effective_message.reply_text("🔎 بدأ الفحص…")
    try:
        candidates = await run_scan()
        if not candidates:
            return await update.effective_message.reply_text("لم يظهر أي مرشح ضمن الفلاتر الحالية.")
        await update.effective_message.reply_text(
            "\n\n".join(candidate_text(c, i) for i, c in enumerate(candidates[:5], 1)),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ فشل الفحص: {exc}")


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await deny(update)
    try:
        candidates = await run_scan()
        await update.effective_message.reply_text(report_text(candidates))
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ تعذر إنشاء التقرير: {exc}")


async def buy_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await deny(update)
    try:
        candidates = await run_scan()
        picks = [x for x in candidates if x.get("signal") == "BUY_WATCH"]
        if not picks:
            return await update.effective_message.reply_text(
                "🟡 حاليًا ما في مرشح استوفى شروط فرصة الشراء. هذا أفضل من إعطاء إشارة ناقصة البيانات."
            )
        parts = []
        for i, c in enumerate(picks[:5], 1):
            parts.append(
                f"{i}. {candidate_text(c)}\n"
                "🛒 الإجراء: راجع الرابط يدويًا قبل أي تنفيذ."
            )
        await update.effective_message.reply_text(
            "\n\n".join(parts),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ تعذر استخراج فرص الشراء: {exc}")


async def services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await deny(update)
    try:
        result = await asyncio.to_thread(engine.diagnose)

        def line(name: str, item: dict) -> str:
            state_name = item.get("state", "UNKNOWN")
            detail = item.get("detail", "لا توجد تفاصيل")
            icon = {
                "OK": "✅",
                "CONFIGURED": "🟡",
                "MISSING": "⚠️",
                "UNAUTHORIZED": "❌",
                "FORBIDDEN": "⛔",
                "RATE_LIMIT": "⏳",
                "NOT_FOUND": "❌",
                "REJECTED": "❌",
                "EMPTY": "⚠️",
                "ERROR": "❌",
            }.get(state_name, "⚪")
            return f"{icon} {name}: {state_name}\n   {detail}"

        await update.effective_message.reply_text(
            "🧪 فحص الخدمات\n\n"
            + "\n\n".join(
                line(label, result.get(key, {}))
                for key, label in (
                    ("dex", "DEX Screener"),
                    ("moralis", "Moralis"),
                    ("goplus", "GoPlus"),
                )
            )
            + "\n\nملاحظة: الفحص يميّز بين المفتاح المفقود، المفتاح المرفوض، وحدّ الطلبات."
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ تعذر فحص الخدمات: {exc}")


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await deny(update)
    try:
        candidates = await run_scan()
        text = "\n".join(
            f"{i}. {c['symbol']} | {c['score']}/100 | {c['chain']}"
            for i, c in enumerate(candidates[:10], 1)
        ) or "لا توجد نتائج."
        await update.effective_message.reply_text("🏆 أعلى المرشحين:\n\n" + text)
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ تعذر الفحص: {exc}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await deny(update)
    provider = getattr(engine, "last_provider_status", {}) or {}

    def provider_line(name: str) -> str:
        item = provider.get(name, {}) or {}
        state_name = item.get("state", "UNKNOWN")
        detail = item.get("detail", "")
        icon = "✅" if state_name == "OK" else "❌" if state_name in {"UNAUTHORIZED", "FORBIDDEN", "ERROR", "REJECTED"} else "⚠️"
        return f"{icon}{name}: {state_name}" + (f" — {detail}" if detail else "")

    scan_meta = getattr(engine, "last_scan", {}) or {}
    await update.effective_message.reply_text(
        "📊 Crypto Radar\n"
        f"الحالة: {'يعمل' if state['running'] else 'متوقف'}\n"
        f"آخر فحص: {state['last_scan_at'] or 'لم يبدأ'}\n"
        f"اكتشف: {scan_meta.get('discovered', 0)} | اجتاز السوق: {scan_meta.get('market_rows', 0)} | النتائج: {scan_meta.get('candidates', 0)}\n"
        f"قاعدة البيانات: {storage.stats()['backend']}\n"
        + provider_line("moralis") + "\n"
        + provider_line("goplus") + "\n"
        + provider_line("dex") + "\n"
        f"آخر خطأ: {state['last_error'] or 'لا يوجد'}"
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await deny(update)
    await update.effective_message.reply_text(
        "🧠 هذا البوت تحول من بوت تداول ذهب إلى Crypto Radar: اكتشاف توكنات صغيرة، "
        "فحص أمان، سيولة وحجم ومشترين، Smart Money، أخبار، ثم درجة تحليلية. "
        "لا يوجد تنفيذ شراء أو بيع تلقائي."
    )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        return await deny(update)
    mapping = {
        "🔎 فحص الآن": scan,
        "🟢 فرص شراء": buy_watch,
        "🏆 الأعلى": top,
        "🌅 التقرير": report,
        "🧪 فحص الخدمات": services,
        "📊 الحالة": status,
        "ℹ️ عن الرادار": about,
        "🚪 خروج": logout,
    }
    fn = mapping.get((update.effective_message.text or "").strip())
    if fn:
        await fn(update, context)


async def scheduled_scan(context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_alerts(context.application, await run_scan())
    except Exception:
        logger.exception("scheduled scan failed")


async def scheduled_daily_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        candidates = await run_scan()
        recipients = sorted(set(authorized) | admin_ids())
        if not recipients:
            return
        message = report_text(candidates)
        for chat_id in recipients:
            try:
                await context.bot.send_message(chat_id, message, disable_web_page_preview=True)
            except Exception:
                logger.exception("daily report delivery failed")
    except Exception:
        logger.exception("daily report failed")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = {
            "status": "ok",
            "service": "crypto-radar",
            "last_scan_at": state["last_scan_at"],
            "last_scan_count": state["last_scan_count"],
            "last_error": state["last_error"],
        }
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    ThreadingHTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    for name, fn in {
        "start": start, "auth": auth, "logout": logout, "scan": scan,
        "report": report, "top": top, "status": status, "about": about,
        "buy_watch": buy_watch, "services": services,
    }.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    if app.job_queue:
        app.job_queue.run_repeating(scheduled_scan, interval=SCAN_INTERVAL, first=15)
        try:
            tz = ZoneInfo(BOT_TIMEZONE)
        except Exception:
            tz = timezone.utc
        app.job_queue.run_daily(
            scheduled_daily_report,
            time=dtime(REPORT_HOUR, REPORT_MINUTE, tzinfo=tz),
            name="daily-report",
        )
    logger.info("Crypto Radar starting")
    logger.info(
        "Provider config: Moralis=%s GoPlus=%s DB=%s scan_interval=%ss",
        bool(os.getenv("MORALIS_API_KEY")),
        bool(os.getenv("GOPLUS_API_KEY") or os.getenv("GOPLUS_ACCESS_TOKEN")),
        storage.stats()["backend"],
        SCAN_INTERVAL,
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
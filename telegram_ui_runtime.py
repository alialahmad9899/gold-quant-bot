"""Telegram UI bridge for the Trade Lawyer and live Gold News features.

The UI is installed after the existing bot module is ready, so it does not
replace the existing signal/analysis pipeline. It only adds Arabic controls
and routes both commands and reply-keyboard buttons to the existing engines.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from typing import Any

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

_INSTALLED = False
_LOCK = threading.Lock()

LAWYER_BUTTON = "⚖️ محامي الصفقة"
NEWS_BUTTON = "📰 أخبار الذهب"


def _arabic_news_text(bot: Any) -> str:
    cache = getattr(bot, "GLOBAL_CACHE", {})
    if not isinstance(cache, dict):
        cache = {}
    health = cache.get("news_health") or {}
    decision = cache.get("news_decision") or {}
    latest = cache.get("latest_news") or []

    lines = ["📰 أخبار الذهب — التحليل اللحظي", ""]
    if decision:
        direction = {
            "BULLISH_GOLD": "إيجابي للذهب 🟢",
            "BEARISH_GOLD": "سلبي للذهب 🔴",
            "NEUTRAL": "محايد 🟡",
        }.get(str(decision.get("direction", "")).upper(), str(decision.get("direction", "غير محدد")))
        lines += [
            f"التأثير الحالي: {direction}",
            f"قوة الخبر: {decision.get('impact', 'غير متاح')}",
            f"الثقة: {decision.get('confidence', 'غير متاح')}%",
        ]
        if decision.get("action"):
            action = {
                "NO_TRADE": "لا دخول حالياً",
                "WAIT_CONFIRMATION": "انتظار تأكيد الحركة",
                "REASSESS": "إعادة تقييم الصفقة",
                "ENTRY": "فرصة دخول مرتبطة بالخبر",
                "HOLD": "الاحتفاظ بالصفقة",
                "EXIT": "الخروج من الصفقة",
                "PREPARE_REVERSAL": "الاستعداد لانعكاس محتمل",
            }.get(str(decision.get("action")).upper(), str(decision.get("action")))
            lines.append(f"قرار المحرك: {action}")
    else:
        lines.append("لا يوجد حالياً حدث إخباري مؤكد يستدعي قراراً فورياً.")

    if latest:
        lines += ["", "آخر الأخبار المؤثرة:"]
        for article in latest[:5]:
            direction = {"BULLISH_GOLD": "🟢", "BEARISH_GOLD": "🔴", "NEUTRAL": "🟡"}.get(str(article.get("direction", "")).upper(), "⚪")
            title = str(article.get("title", "خبر"))[:110]
            lines.append(f"{direction} {title}")
    else:
        lines += ["", "لا توجد أخبار حديثة متاحة في الكاش حالياً."]

    if health:
        lines += [
            "",
            f"حالة محرك الأخبار: {'🟢 يعمل' if health.get('last_success') else '🟡 بانتظار التحديث'}",
            f"المصادر المقروءة: {health.get('articles', 0)}",
            f"الأحداث المجمعة: {health.get('event_clusters', 0)}",
        ]
    lines += ["", "ملاحظة: الخبر لا يدخل الصفقة وحده؛ يتم تأكيده مع حركة السعر وباقي محركات المخاطر."]
    return "\n".join(lines)


def _arabic_lawyer_text(bot: Any) -> str:
    cache = getattr(bot, "GLOBAL_CACHE", {})
    lawyer = cache.get("trade_lawyer") if isinstance(cache, dict) else None
    if not lawyer:
        try:
            integration = getattr(bot, "PHASE2_RUNTIME", None) or getattr(bot, "phase2_runtime", None)
            if integration is not None and integration.manager.has_active_trade():
                result = integration.review_active_trade()
                lawyer = result.get("lawyer")
        except Exception:
            lawyer = None

    if not lawyer:
        return "⚖️ محامي الصفقة\n\nلا توجد حالياً صفقة نشطة تحتاج إلى مراجعة.\n\nالمحامي يعمل تلقائياً عند فتح صفقة، ويراقبها أثناء حركتها."

    action_map = {
        "HOLD": "الاحتفاظ بالصفقة 🟢",
        "PROTECT_PROFIT": "حماية الأرباح 🔒",
        "REDUCE_RISK": "تخفيف المخاطرة ⚠️",
        "ADD_ON_CONFIRMATION": "تعزيز فقط بعد التأكيد ➕",
        "EXIT": "الخروج من الصفقة 🛑",
        "PREPARE_REVERSAL": "الاستعداد للانعكاس 🔄",
    }
    action = action_map.get(str(lawyer.get("action", "HOLD")).upper(), str(lawyer.get("action", "HOLD")))
    return "\n".join([
        "⚖️ محامي الصفقة",
        "",
        f"القرار: {action}",
        f"السبب: {lawyer.get('reason', 'لا يوجد سبب إضافي')}",
        f"حالة الصفقة: {lawyer.get('state', lawyer.get('status', 'نشطة'))}",
        "",
        "المحامي لا يفتح صفقة من تلقاء نفسه؛ وظيفته متابعة الصفقة النشطة وتقديم توصية عند تغير الحالة أو الخبر أو البنية.",
    ])


async def _lawyer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = sys.modules.get("bot") or sys.modules.get("__main__")
    await update.effective_message.reply_text(_arabic_lawyer_text(bot))


async def _news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = sys.modules.get("bot") or sys.modules.get("__main__")
    await update.effective_message.reply_text(_arabic_news_text(bot))


async def _lawyer_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = sys.modules.get("bot") or sys.modules.get("__main__")
    await update.effective_message.reply_text(_arabic_lawyer_text(bot))


async def _news_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = sys.modules.get("bot") or sys.modules.get("__main__")
    await update.effective_message.reply_text(_arabic_news_text(bot))


def _application(bot: Any) -> Any | None:
    for name in ("application", "app", "telegram_application", "telegram_app"):
        value = getattr(bot, name, None)
        if value is not None and hasattr(value, "add_handler"):
            return value
    return None


def _keyboard_with_controls(original_markup: Any) -> ReplyKeyboardMarkup:
    rows = []
    if isinstance(original_markup, ReplyKeyboardMarkup):
        rows = [list(row) for row in (original_markup.keyboard or [])]
    # Do not duplicate controls if the installer is called more than once.
    labels = {getattr(button, "text", "") for row in rows for button in row}
    if LAWYER_BUTTON not in labels or NEWS_BUTTON not in labels:
        rows += [[KeyboardButton(LAWYER_BUTTON), KeyboardButton(NEWS_BUTTON)]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def install(bot: Any) -> bool:
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return True
        original = getattr(bot, "get_main_keyboard", None)
        if original is not None and not getattr(original, "_arabic_trade_controls", False):
            def keyboard_wrapper(*args: Any, **kwargs: Any):
                return _keyboard_with_controls(original(*args, **kwargs))
            keyboard_wrapper._arabic_trade_controls = True
            bot.get_main_keyboard = keyboard_wrapper

        app = _application(bot)
        if app is not None:
            app.add_handler(CommandHandler("lawyer", _lawyer_command), group=0)
            app.add_handler(CommandHandler("goldnews", _news_command), group=0)
            app.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{LAWYER_BUTTON}$"), _lawyer_button), group=0)
            app.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{NEWS_BUTTON}$"), _news_button), group=0)

        cache = getattr(bot, "GLOBAL_CACHE", None)
        if isinstance(cache, dict):
            cache["telegram_ui"] = {
                "installed": True,
                "lawyer_button": LAWYER_BUTTON,
                "news_button": NEWS_BUTTON,
                "arabic": True,
            }
        _INSTALLED = True
        return True


def start(timeout_seconds: float = 90.0) -> threading.Thread:
    def worker() -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            bot = sys.modules.get("bot") or sys.modules.get("__main__")
            if bot is not None and hasattr(bot, "get_main_keyboard"):
                try:
                    if install(bot):
                        return
                except Exception:
                    pass
            time.sleep(0.25)
    thread = threading.Thread(target=worker, name="arabic-telegram-ui-bootstrap", daemon=True)
    thread.start()
    return thread

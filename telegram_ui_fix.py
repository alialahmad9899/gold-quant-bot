"""Telegram UI routing hardening for the canonical buttons."""
from __future__ import annotations
import logging
LOGGER=logging.getLogger("XAUUSD_QuantBot.TelegramUI")
_INSTALLED=False

async def _button_message(update, context):
    from telegram.ext import ApplicationHandlerStop
    text=(update.message.text or "").strip(); bot=context.application.bot
    if text=="🧑‍⚖️ محامي الصفقة":
        await bot._decision_layer_ui_lawyer(update,context); raise ApplicationHandlerStop
    if text=="📰 أخبار الذهب":
        await bot._decision_layer_ui_news(update,context); raise ApplicationHandlerStop

def install(bot):
    global _INSTALLED
    if _INSTALLED: return
    _INSTALLED=True
    original=getattr(bot,"post_init",None)
    if original is None: return
    if getattr(original,"_telegram_ui_hardening",False): return
    async def post_init(app):
        try:
            from telegram.ext import MessageHandler, filters
            async def lawyer(update,context):
                from decision_layer import _lawyer_handler
                await _lawyer_handler(update,context)
            async def news(update,context):
                from decision_layer import _news_handler
                await _news_handler(update,context)
            bot._decision_layer_ui_lawyer=lawyer; bot._decision_layer_ui_news=news
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,_button_message),group=-100)
            app._telegram_ui_hardening=True
        except Exception as exc: LOGGER.exception("Telegram button routing failed: %s",exc)
        await original(app)
    post_init._telegram_ui_hardening=True; bot.post_init=post_init

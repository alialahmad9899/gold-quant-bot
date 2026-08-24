"""Operational diagnostics and the single news context bridge for the canonical decision stack."""
from __future__ import annotations
import logging
LOGGER=logging.getLogger("XAUUSD_QuantBot.RuntimeDiagnostics")
_INSTALLED=False

def _base_lawyer(bot):
    try:
        import decision_layer
        return decision_layer._lawyer_snapshot(bot)
    except Exception:
        return "🧑‍⚖️ تعذر تحميل حالة محامي الصفقة حالياً."

def _latest_news_conflict(bot):
    try:
        cache=getattr(bot,"GLOBAL_CACHE",{}) or {}; events=list(cache.get("latest_news") or []); conn=bot.get_db_connection()
        try:
            cur=conn.cursor(); cur.execute("SELECT signal_type FROM trades WHERE outcome IS NULL AND trade_status IN ('OPEN','TP1_HIT') ORDER BY id DESC LIMIT 1"); row=cur.fetchone()
        finally:
            try: bot.release_db_connection(conn)
            except Exception: pass
        if not row or not events: return None
        raw=str(row[0] or "").upper(); direction="BUY" if "BUY" in raw or "شراء" in raw else "SELL"
        for ev in sorted(events,key=lambda x:(int(x.get("impact",0)),int(x.get("confidence",0))),reverse=True):
            nd=str(ev.get("direction") or "NEUTRAL"); conflict=(direction=="BUY" and nd=="BEARISH_GOLD") or (direction=="SELL" and nd=="BULLISH_GOLD")
            if conflict and int(ev.get("impact",0))>=60:
                reaction=ev.get("reaction") or {}; confirmed=bool(reaction.get("confirmed")); impact=int(ev.get("impact",0)); return {"title":ev.get("title"),"impact":impact,"confirmed":confirmed,"action":"EXIT" if confirmed and impact>=75 else "REDUCE_RISK"}
    except Exception as exc: LOGGER.debug("news conflict: %s",exc)
    return None

def patch_lawyer(bot):
    def snapshot():
        base=_base_lawyer(bot); conflict=_latest_news_conflict(bot)
        if not conflict: return base
        return base+f"\n\n📰 تحديث الأخبار: خبر مؤثر ضد الصفقة ({conflict['title']}) بتأثير {conflict['impact']}/100.\nالحكم المعدّل: {conflict['action']}\nالخبر {'تأكد سعرياً' if conflict['confirmed'] else 'لم يتأكد سعرياً بالكامل'}؛ لا يوجد خروج تلقائي من العنوان وحده."
    bot._decision_layer_lawyer_snapshot=snapshot

def patch_health(bot):
    original=getattr(bot,"system_health_check",None)
    if original is None or getattr(original,"_diagnostic_patch",False): return
    async def health(update,context):
        try:
            import decision_layer,news_runtime
            d=decision_layer.health(); news=news_runtime.health(); cache=getattr(bot,"GLOBAL_CACHE",{}) or {}; nh=cache.get("news_health") or {}; bal=cache.get("direction_balance")
            await bot.safe_reply_text(update,(f"🧠 صحة القرار: {'🟢 تعمل' if d.get('patched') else '🟠 قيد التثبيت'}\n" f"📰 صحة الأخبار: آخر فحص {nh.get('last_success') or 'غير متوفر'} | مجموعات الأحداث {nh.get('event_clusters',0)} | التأكيدات {nh.get('confirmed_material',0)}\n" f"🧑‍⚖️ صحة المحامي: مربوط بمسار القرار الموحد.\n" f"📐 توازن الاتجاه: {(f'BUY {bal.get('buy_pct')}% / SELL {bal.get('sell_pct')}%' if isinstance(bal,dict) else 'لا توجد عينة بعد')}") ,reply_markup=bot.get_main_keyboard())
        except Exception: pass
        await original(update,context)
    health._diagnostic_patch=True; bot.system_health_check=health

def install(bot):
    global _INSTALLED
    if _INSTALLED: return
    _INSTALLED=True
    patch_lawyer(bot); patch_health(bot)

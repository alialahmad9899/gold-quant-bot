"""Operational diagnostics and the single news context bridge for the canonical decision stack."""
from __future__ import annotations
import logging, threading, time
from typing import Any
LOGGER=logging.getLogger("XAUUSD_QuantBot.RuntimeDiagnostics")
_INSTALLED=False

def _latest_news_conflict(bot):
    try:
        runtime=getattr(bot,"_news_runtime",None); cache=getattr(bot,"GLOBAL_CACHE",{}) or {}; events=list(cache.get("latest_news") or [])
        if not events: return None
        trade_dir=None; conn=bot.get_db_connection()
        try:
            cur=conn.cursor(); cur.execute("SELECT signal_type FROM trades WHERE outcome IS NULL AND trade_status IN ('OPEN','TP1_HIT') ORDER BY id DESC LIMIT 1"); row=cur.fetchone(); trade_dir=str(row[0] or "").upper() if row else None
        finally:
            try: bot.release_db_connection(conn)
            except Exception: pass
        if not trade_dir: return None
        direction="BUY" if "BUY" in trade_dir or "شراء" in trade_dir else "SELL"
        for ev in sorted(events,key=lambda x:(x.get("impact",0),x.get("confidence",0)),reverse=True):
            news_dir=str(ev.get("direction") or "NEUTRAL")
            conflict=(direction=="BUY" and news_dir=="BEARISH_GOLD") or (direction=="SELL" and news_dir=="BULLISH_GOLD")
            if conflict and int(ev.get("impact",0))>=60:
                reaction=ev.get("reaction") or {}
                confirmed=bool(reaction.get("confirmed"))
                return {"trade_direction":direction,"news_direction":news_dir,"impact":ev.get("impact"),"confidence":ev.get("confidence"),"confirmed":confirmed,"title":ev.get("title"),"action":"EXIT" if confirmed and int(ev.get("impact",0))>=75 else "REDUCE_RISK"}
    except Exception as exc: LOGGER.debug("news conflict: %s",exc)
    return None

def patch_lawyer(bot):
    original=getattr(bot,"_decision_layer_lawyer_snapshot",None)
    if original is None: return
    if getattr(original,"_news_bridge",False): return
    def snapshot():
        base=original(); conflict=_latest_news_conflict(bot)
        if not conflict: return base
        return base+f"\n\n📰 تحديث الأخبار: ظهر خبر مؤثر ضد الصفقة ({conflict['title']}) بتأثير {conflict['impact']}/100.\nالحكم المعدّل: {conflict['action']}\nالتفسير: الخبر {'تأكد سعرياً' if conflict['confirmed'] else 'لم يتأكد سعرياً بالكامل'}؛ لذلك لا يتم اعتبار الخبر وحده سبباً للخروج إلا عند التأكيد القوي."
    snapshot._news_bridge=True; bot._decision_layer_lawyer_snapshot=snapshot

def patch_health(bot):
    original=getattr(bot,"system_health_check",None)
    if original is None or getattr(original,"_diagnostic_patch",False): return
    async def health(update,context):
        try:
            import decision_layer, news_runtime
            d=decision_layer.health(); n=news_runtime.health(); cache=getattr(bot,"GLOBAL_CACHE",{}) or {}
            news=cache.get("news_health") or {}; balance=cache.get("direction_balance")
            extra=("\n\n🧠 **صحة القرار:** " + ("🟢 تعمل" if d.get("patched") else "🟠 قيد التثبيت") + f"\n📰 **صحة الأخبار:** آخر فحص {news.get('last_success') or 'غير متوفر'} | مجموعات الأحداث {news.get('event_clusters',0)} | تأكيدات {news.get('confirmed_material',0)}\n🧑‍⚖️ **صحة المحامي:** متصل بمسار القرار الموحد.\n📐 **توازن الاتجاه:** " + (f"BUY {balance.get('buy_pct')}% / SELL {balance.get('sell_pct')}%" if isinstance(balance,dict) else "لا توجد عينة بعد"))
            await bot.safe_reply_text(update,extra,reply_markup=bot.get_main_keyboard())
            await original(update,context)
        except Exception: await original(update,context)
    health._diagnostic_patch=True; bot.system_health_check=health

def install(bot):
    global _INSTALLED
    if _INSTALLED: return
    _INSTALLED=True
    try:
        lawyer=getattr(bot,"_decision_layer_lawyer_snapshot",None)
        if lawyer: patch_lawyer(bot)
        patch_health(bot)
    except Exception as exc: LOGGER.warning("diagnostic patch failed: %s",exc)

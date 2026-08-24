"""Canonical safety bootstrap.

Hard safety is limited to stale/invalid execution data and objectively invalid
trade geometry. Gemini is advisory by default and the canonical decision layer
owns the final policy. News runtime is started independently for timestamped
reaction tracking and event clustering.
"""
from __future__ import annotations
import logging, os, threading
from datetime import datetime, timezone
from typing import Any
try:
    import psycopg2
except Exception:
    psycopg2 = None
LOGGER=logging.getLogger("XAUUSD_QuantBot.SignalSafety")
_LOCK=threading.RLock(); _INSTALLED=False
MAX_SIGNAL_FEED_AGE_SECONDS=float(os.getenv("SIGNAL_MAX_PRICE_AGE_SECONDS","120"))
GEMINI_HARD_VETO=os.getenv("SIGNAL_SAFETY_GEMINI_HARD_VETO","0") == "1"
MIN_CONFIDENCE=float(os.getenv("FINAL_MIN_CONFIDENCE","0.25")); MIN_RR=float(os.getenv("FINAL_MIN_RR","1.20")); MIN_STOP_PCT=float(os.getenv("FINAL_MIN_STOP_PCT","0.00120"))

def _parse_dt(value):
    try:
        x=datetime.fromisoformat(str(value).replace("Z","+00:00")); return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    except Exception: return None

def _safe_feed(original,bot):
    try:
        runtime=getattr(bot,"_twelve_data_runtime",None); live=runtime.get_websocket_quote(max_age_seconds=MAX_SIGNAL_FEED_AGE_SECONDS) if runtime else None
    except Exception: live=None
    if live:
        price=float(live.get("mid") or live.get("price") or live.get("spot")); live=dict(live); live.update({"status":"ACTIVE","mid":price,"spot":price,"bid":float(live.get("bid") or price),"ask":float(live.get("ask") or price),"signal_safe":True}); return live
    feed=original()
    if not feed: return feed
    safe=dict(feed); provider=str(safe.get("provider") or ""); age=safe.get("age_seconds")
    if age is None and (safe.get("source_timestamp") or safe.get("timestamp")):
        dt=_parse_dt(safe.get("source_timestamp") or safe.get("timestamp")); age=(datetime.now(timezone.utc)-dt).total_seconds() if dt else None
    if "M15 Close" in provider:
        safe.update({"status":"STALE","signal_safe":False,"error_type":"historical_fallback_blocked","error_message":"Historical M15 close cannot authorize a live trade."}); return safe
    if age is not None and float(age)>MAX_SIGNAL_FEED_AGE_SECONDS:
        safe.update({"status":"STALE","signal_safe":False,"error_type":"live_feed_stale","error_message":f"Live XAU/USD feed is {float(age):.1f}s old."}); return safe
    safe["signal_safe"]=safe.get("status")=="ACTIVE"; return safe

def _patch_feed(bot):
    original=getattr(bot,"fetch_canonical_xauusd_feed",None)
    if original is None or getattr(original,"_canonical_safety",False): return
    def wrapped(): return _safe_feed(original,bot)
    wrapped._canonical_safety=True; bot.fetch_canonical_xauusd_feed=wrapped

def _patch_gemini(bot):
    original=getattr(bot,"gemini_verify_signal",None)
    if original is None or getattr(original,"_canonical_safety",False): return
    def advisory(signal_data,market_summary):
        try: raw=original(signal_data,market_summary)
        except Exception as exc: raw={"approved":False,"reason":f"تعذر تنفيذ مراجعة Gemini: {type(exc).__name__}: {exc}"}
        if not isinstance(raw,dict): raw={"approved":False,"reason":"رد Gemini غير صالح."}
        original_approved=bool(raw.get("approved",False)); reason=str(raw.get("reason") or "").strip() or ("موافقة Gemini" if original_approved else "تحفظ Gemini")
        if GEMINI_HARD_VETO:
            return {**raw,"approved":original_approved,"original_approved":original_approved,"advisory":False,"hard_veto":not original_approved,"reason":reason}
        return {**raw,"approved":True,"original_approved":original_approved,"advisory":not original_approved,"hard_veto":False,"reason":reason}
    advisory._canonical_safety=True; bot.gemini_verify_signal=advisory; bot._raw_gemini_verify_signal=original

def _patch_log_trade(bot):
    original=getattr(bot,"log_trade",None)
    if original is None or getattr(original,"_canonical_safety",False): return
    def guarded(*args,**kwargs):
        vals=list(args); direction=str(vals[0] if len(vals)>0 else kwargs.get("signal_type") or "").upper(); entry=vals[1] if len(vals)>1 else kwargs.get("entry_price"); sl=vals[2] if len(vals)>2 else kwargs.get("sl"); tp1=vals[3] if len(vals)>3 else kwargs.get("tp1"); confidence=vals[10] if len(vals)>10 else kwargs.get("confidence",1.0)
        try:
            entry=float(entry); sl=float(sl); tp1=float(tp1); confidence=float(confidence); risk=abs(entry-sl); reward=(tp1-entry) if "BUY" in direction or "شراء" in direction else (entry-tp1); rr=reward/risk if risk>0 else 0.0
            if confidence<MIN_CONFIDENCE or rr<MIN_RR or risk/entry<MIN_STOP_PCT: return False,None
        except Exception: return False,None
        return original(*args,**kwargs)
    guarded._canonical_safety=True; bot.log_trade=guarded

def install_signal_safety(bot:Any|None=None):
    global _INSTALLED
    with _LOCK:
        if _INSTALLED: return
        if bot is None:
            import sys; bot=sys.modules.get("bot") or sys.modules.get("__main__")
        if bot is None: return
        _patch_feed(bot); _patch_gemini(bot); _patch_log_trade(bot)
        try:
            import decision_layer; bot._decision_layer=decision_layer; decision_layer.install(bot)
        except Exception as exc: LOGGER.exception("❌ Decision layer installation failed: %s",exc)
        try:
            import news_runtime; news_runtime.start(bot); bot._news_runtime=news_runtime
        except Exception as exc: LOGGER.exception("❌ News runtime installation failed: %s",exc)
        _INSTALLED=True; LOGGER.info("✅ Canonical safety installed: Gemini advisory by default; decision layer + news runtime own final policy.")

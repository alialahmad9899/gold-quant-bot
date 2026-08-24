"""Bridge the canonical final signal into the existing trade ledger.

The legacy generator previously logged trades itself. The canonical decision layer
replaces that generator, so this adapter preserves the exact existing log_trade
contract without duplicating strategy calculations.
"""
from __future__ import annotations
import logging, threading
LOGGER=logging.getLogger("XAUUSD_QuantBot.ExecutionBridge")
_LOCK=threading.RLock(); _INSTALLED=False

def install(bot):
    global _INSTALLED
    with _LOCK:
        if _INSTALLED: return
        original=getattr(bot,"generate_quant_signal",None)
        log_trade=getattr(bot,"log_trade",None)
        if original is None or log_trade is None:
            LOGGER.error("❌ Execution bridge requires generate_quant_signal and log_trade")
            return
        if getattr(original,"_execution_bridge",False): _INSTALLED=True; return
        def wrapped(*args,**kwargs):
            result=original(*args,**kwargs)
            if not isinstance(result,dict) or result.get("status")!="SIGNAL": return result
            candle_id=str(result.get("candle_id") or "")
            signal_type=str(result.get("type") or "")
            try:
                confidence=float(result.get("confidence") or 0.0)/100.0
                entry=float(result["entry"]); sl=float(result["sl"]); tp1=float(result["tp1"]); tp2=float(result["tp2"])
                rsi=float(result.get("rsi") or 50.0); dxy=float(result.get("dxy_corr") if result.get("dxy_corr") is not None else 0.0); macd=float(result.get("macd_diff") or 0.0); stoch=float(result.get("stoch_k") or 50.0); vol=float(result.get("volatility_ratio") or 0.0); score=float(max(result.get("score_bull",0.0),result.get("score_bear",0.0))); ai_score=float(result.get("ai_score") or 0.0)
                inserted, trade_id=log_trade(signal_type,entry,sl,tp1,tp2,rsi,dxy,macd,stoch,vol,confidence,candle_id=candle_id,signal_score=score,ai_score=ai_score)
                if not inserted:
                    return {"status":"WAIT","decision_state":"NOT_RECORDED","reason":"تم منع تسجيل الإشارة لأنها مكررة أو لم تجتز سلامة سجل الصفقة.","price":entry,"candle_id":candle_id}
                result=dict(result); result["trade_id"]=trade_id; result["ledger_recorded"]=True; return result
            except Exception as exc:
                LOGGER.exception("❌ Failed to persist final signal %s: %s",candle_id,exc)
                return {"status":"WAIT","decision_state":"NOT_RECORDED","reason":f"تعذر تسجيل الإشارة في سجل الصفقات: {type(exc).__name__}: {exc}","price":result.get("entry",0.0),"candle_id":candle_id}
        wrapped._execution_bridge=True; bot.generate_quant_signal=wrapped; _INSTALLED=True; LOGGER.info("✅ Execution bridge installed: final signals are persisted for Trade Lawyer/stats.")

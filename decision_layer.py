"""Canonical final-decision and runtime calibration layer for XAU/USD."""
from __future__ import annotations
import hashlib, json, logging, os, threading, time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from collections import deque
import numpy as np
import pandas as pd
import requests
import ta

LOGGER = logging.getLogger("XAUUSD_QuantBot.DecisionLayer")
_LOCK = threading.RLock(); _INSTALLED=False; _PATCHED=False
_DXY_STATE={"symbol":None,"updated":0.0,"df":pd.DataFrame(),"quote":None,"error":None}
_PRICE_HISTORY=deque(maxlen=3000)
_LAST_LAWYER_ALERT=("",0.0)
_NEWS_ENGINE=None
H4_EMA_FAST=int(os.getenv("CANONICAL_H4_EMA_FAST","50")); H4_EMA_SLOW=int(os.getenv("CANONICAL_H4_EMA_SLOW","200"))
MIN_CONFIDENCE=float(os.getenv("FINAL_MIN_CONFIDENCE","0.25")); LOW_CONFIDENCE=float(os.getenv("FINAL_LOW_CONFIDENCE","0.40"))
APPROVE_SCORE=float(os.getenv("FINAL_APPROVE_SCORE","4.50")); MIN_DIRECTION_MARGIN=float(os.getenv("FINAL_DIRECTION_MARGIN","0.55"))
LOW_CONFIDENCE_SCORE=float(os.getenv("FINAL_LOW_CONFIDENCE_SCORE","6.0")); MIN_RR=float(os.getenv("FINAL_MIN_RR","1.20"))
PREFERRED_RR1=float(os.getenv("FINAL_TARGET_RR1","1.45")); PREFERRED_RR2=float(os.getenv("FINAL_TARGET_RR2","2.20"))
MIN_STOP_PCT=float(os.getenv("FINAL_MIN_STOP_PCT","0.00120")); SETUP_DEDUP_MINUTES=float(os.getenv("FINAL_SETUP_DEDUP_MINUTES","45"))
PRICE_BUCKET_PCT=float(os.getenv("FINAL_PRICE_BUCKET_PCT","0.0010")); LAWYER_COOLDOWN_SECONDS=float(os.getenv("LAWYER_ALERT_COOLDOWN","900"))

@dataclass
class Decision:
    decision:str; direction:str; confidence:int; risk_score:int; bull_score:float; bear_score:float; margin:float
    reason:str; ai_advisory:str; ai_approved:bool|None; h4_trend:str; hmm_state:str; dxy_corr:float|None
    dxy_trend:str; dxy_pressure:str; rr:float; stop_distance:float; final_decision:str; candidate:bool=True
    def to_dict(self): return asdict(self)

def _is_pg(bot, conn):
    try:
        import psycopg2
        return bool(getattr(bot,"is_postgres",lambda:False)() and isinstance(conn,psycopg2.extensions.connection))
    except Exception: return False

def _ensure_schema(bot):
    conn=None
    try:
        conn=bot.get_db_connection(); pg=_is_pg(bot,conn); cur=conn.cursor()
        stmts=(
          ["CREATE TABLE IF NOT EXISTS decision_audit (id BIGSERIAL PRIMARY KEY,candle_id TEXT,direction TEXT,final_decision TEXT,confidence REAL,risk_score REAL,bull_score REAL,bear_score REAL,margin REAL,h4_trend TEXT,hmm_state TEXT,dxy_corr REAL,dxy_trend TEXT,dxy_pressure TEXT,rr REAL,reason TEXT,ai_approved INTEGER,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)","CREATE INDEX IF NOT EXISTS idx_decision_audit_created ON decision_audit(created_at)","CREATE INDEX IF NOT EXISTS idx_decision_audit_direction ON decision_audit(direction,created_at)","CREATE TABLE IF NOT EXISTS signal_setup_dedupe (setup_key TEXT PRIMARY KEY,direction TEXT,candle_id TEXT,price REAL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"]
          if pg else
          ["CREATE TABLE IF NOT EXISTS decision_audit (id INTEGER PRIMARY KEY AUTOINCREMENT,candle_id TEXT,direction TEXT,final_decision TEXT,confidence REAL,risk_score REAL,bull_score REAL,bear_score REAL,margin REAL,h4_trend TEXT,hmm_state TEXT,dxy_corr REAL,dxy_trend TEXT,dxy_pressure TEXT,rr REAL,reason TEXT,ai_approved INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP)","CREATE INDEX IF NOT EXISTS idx_decision_audit_created ON decision_audit(created_at)","CREATE INDEX IF NOT EXISTS idx_decision_audit_direction ON decision_audit(direction,created_at)","CREATE TABLE IF NOT EXISTS signal_setup_dedupe (setup_key TEXT PRIMARY KEY,direction TEXT,candle_id TEXT,price REAL,created_at TEXT DEFAULT CURRENT_TIMESTAMP"])
        )
        for s in stmts: cur.execute(s)
        conn.commit()
    except Exception as exc:
        LOGGER.warning("[DECISION_SCHEMA] %s",exc)
        try:
            if conn: conn.rollback()
        except Exception: pass
    finally:
        if conn is not None:
            try: bot.release_db_connection(conn)
            except Exception:
                try: conn.close()
                except Exception: pass

def _save_audit(bot,p):
    conn=None
    try:
        conn=bot.get_db_connection(); pg=_is_pg(bot,conn); cur=conn.cursor(); ph="%s" if pg else "?"
        keys=("candle_id","direction","final_decision","confidence","risk_score","bull_score","bear_score","margin","h4_trend","hmm_state","dxy_corr","dxy_trend","dxy_pressure","rr","reason","ai_approved")
        vals=tuple(p.get(k) for k in keys); cur.execute(f"INSERT INTO decision_audit({','.join(keys)}) VALUES ({','.join([ph]*len(keys))})",vals); conn.commit()
    except Exception: 
        try:
            if conn: conn.rollback()
        except Exception: pass
    finally:
        if conn is not None:
            try: bot.release_db_connection(conn)
            except Exception:
                try: conn.close()
                except Exception: pass

def _find_setup(bot,key):
    conn=None
    try:
        conn=bot.get_db_connection(); pg=_is_pg(bot,conn); cur=conn.cursor()
        if pg: cur.execute("SELECT 1 FROM signal_setup_dedupe WHERE setup_key=%s AND created_at >= NOW() - INTERVAL '45 minutes' LIMIT 1",(key,))
        else: cur.execute("SELECT 1 FROM signal_setup_dedupe WHERE setup_key=? AND datetime(created_at) >= datetime('now','-45 minutes') LIMIT 1",(key,))
        return cur.fetchone() is not None
    except Exception: return False
    finally:
        if conn is not None:
            try: bot.release_db_connection(conn)
            except Exception:
                try: conn.close()
                except Exception: pass

def _remember_setup(bot,key,direction,candle,price):
    conn=None
    try:
        conn=bot.get_db_connection(); pg=_is_pg(bot,conn); cur=conn.cursor()
        if pg: cur.execute("INSERT INTO signal_setup_dedupe(setup_key,direction,candle_id,price) VALUES (%s,%s,%s,%s) ON CONFLICT(setup_key) DO UPDATE SET created_at=CURRENT_TIMESTAMP,candle_id=EXCLUDED.candle_id,price=EXCLUDED.price",(key,direction,candle,price))
        else: cur.execute("INSERT INTO signal_setup_dedupe(setup_key,direction,candle_id,price) VALUES (?,?,?,?) ON CONFLICT(setup_key) DO UPDATE SET created_at=CURRENT_TIMESTAMP,candle_id=excluded.candle_id,price=excluded.price",(key,direction,candle,price))
        conn.commit()
    except Exception:
        try:
            if conn: conn.rollback()
        except Exception: pass
    finally:
        if conn is not None:
            try: bot.release_db_connection(conn)
            except Exception:
                try: conn.close()
                except Exception: pass

def _resample_h4(df):
    if df is None or df.empty: return pd.DataFrame()
    x=df.copy().sort_index()
    if not isinstance(x.index,pd.DatetimeIndex): x.index=pd.to_datetime(x.index,utc=True)
    return pd.DataFrame({"Open":x["Open"].resample("4h",label="left",closed="left").first(),"High":x["High"].resample("4h",label="left",closed="left").max(),"Low":x["Low"].resample("4h",label="left",closed="left").min(),"Close":x["Close"].resample("4h",label="left",closed="left").last()}).dropna()

def _canonical_dxy_symbol(api_key):
    if _DXY_STATE.get("symbol"): return _DXY_STATE["symbol"]
    try:
        r=requests.get(f"https://api.twelvedata.com/symbol_search?symbol={quote('US Dollar Index')}&apikey={api_key}",timeout=6); data=r.json() if r.status_code==200 else {}
        for item in data.get("data") or []:
            name=str(item.get("instrument_name") or item.get("name") or "").lower(); sym=str(item.get("symbol") or "").strip()
            if sym and ("dollar index" in name or sym.upper()=="DXY"):
                _DXY_STATE["symbol"]=sym; return sym
    except Exception as exc: _DXY_STATE["error"]=str(exc)
    return None

def _fetch_dxy(api_key):
    now=time.monotonic()
    if now-float(_DXY_STATE.get("updated") or 0)<300 and not _DXY_STATE.get("df",pd.DataFrame()).empty: return _DXY_STATE["df"].copy(),dict(_DXY_STATE.get("quote") or {})
    sym=_canonical_dxy_symbol(api_key)
    if not sym: return pd.DataFrame(),{}
    try:
        r=requests.get(f"https://api.twelvedata.com/time_series?symbol={quote(sym)}&interval=15min&outputsize=120&apikey={api_key}",timeout=8); data=r.json() if r.status_code==200 else {}; vals=data.get("values") or []
        if not vals: return pd.DataFrame(),{}
        x=pd.DataFrame(vals); x["datetime"]=pd.to_datetime(x["datetime"],utc=True); x=x.set_index("datetime").sort_index(); x["close"]=pd.to_numeric(x["close"],errors="coerce"); x=x.dropna(subset=["close"])
        last=float(x["close"].iloc[-1]); prev=float(x["close"].iloc[-5]) if len(x)>=5 else float(x["close"].iloc[0]); ch=(last-prev)/prev*100 if prev else 0.0
        q={"symbol":sym,"value":last,"change_pct":ch,"trend":"BULLISH" if ch>0.03 else "BEARISH" if ch<-0.03 else "NEUTRAL","timestamp":x.index[-1].isoformat()}
        _DXY_STATE.update({"df":x,"quote":q,"updated":now,"error":None}); return x.copy(),q
    except Exception as exc: _DXY_STATE["error"]=str(exc); return pd.DataFrame(),{}

def _macro(bot, gold_df):
    key=str(os.getenv("TWELVE_DATA_API_KEY","")).strip()
    if not key: return {"corr":None,"trend":"UNKNOWN","pressure":"NEUTRAL","value":None}
    dxy,q=_fetch_dxy(key)
    if dxy.empty: return {"corr":None,"trend":"UNKNOWN","pressure":"NEUTRAL","value":None}
    gr=np.log(gold_df["Close"].astype(float)/gold_df["Close"].astype(float).shift(1)); dr=np.log(dxy["close"].astype(float)/dxy["close"].astype(float).shift(1)).reindex(gr.index).ffill(); cs=gr.rolling(20).corr(dr).dropna(); corr=float(cs.iloc[-1]) if not cs.empty else None
    tr=q.get("trend","NEUTRAL"); pressure="BULLISH_GOLD" if tr=="BEARISH" else "BEARISH_GOLD" if tr=="BULLISH" else "NEUTRAL"
    return {"corr":corr,"trend":tr,"pressure":pressure,"value":q.get("value")}

def _patch_analysis(bot):
    orig=getattr(bot,"analyze_institutional_engine",None)
    if orig is None or getattr(orig,"_canonical_decision_layer",False): return
    def wrapped():
        r=orig()
        if not r: return r
        try:
            cache=bot.get_chart_data_cached(); h4=_resample_h4(cache.get("df_gold_h1"))
            if len(h4)>=30:
                f=ta.trend.EMAIndicator(h4["Close"],window=min(H4_EMA_FAST,len(h4)-1)).ema_indicator().dropna(); s=ta.trend.EMAIndicator(h4["Close"],window=min(H4_EMA_SLOW,len(h4)-1)).ema_indicator().dropna()
                if not f.empty and not s.empty: r["h4_trend"]="BULLISH" if f.iloc[-1]>s.iloc[-1] else "BEARISH"
                r["h4_candle_time"]=h4.index[-1].isoformat()
            m=_macro(bot,r["df_m15"]); r.update({"dxy_corr":m["corr"],"dxy_trend":m["trend"],"dxy_pressure":m["pressure"],"dxy_value":m["value"],"analysis_version":"canonical-h4-v1"})
        except Exception as exc: r["analysis_warning"]=str(exc)
        return r
    wrapped._canonical_decision_layer=True; bot.analyze_institutional_engine=wrapped

def _score(data,ema_f,ema_s,confidence):
    h4=str(data.get("h4_trend") or "UNKNOWN"); hmm=str(data.get("state_label") or "RANGING"); smc=data.get("smc") or {}; macro=str(data.get("dxy_pressure") or "NEUTRAL"); bull=bear=0.0; rb=[]; rs=[]
    if h4=="BULLISH": bull+=2.0; rb.append("H4 صاعد")
    elif h4=="BEARISH": bear+=2.0; rs.append("H4 هابط")
    if hmm=="BULLISH": bull+=1.25; rb.append("HMM صاعد")
    elif hmm=="BEARISH": bear+=1.25; rs.append("HMM هابط")
    if ema_f>ema_s: bull+=1.10; rb.append("زخم M15 صاعد")
    elif ema_f<ema_s: bear+=1.10; rs.append("زخم M15 هابط")
    if smc.get("fvg_bullish") or smc.get("sweep_bullish"): bull+=1.75; rb.append("SMC صاعد")
    if smc.get("fvg_bearish") or smc.get("sweep_bearish"): bear+=1.75; rs.append("SMC هابط")
    if macro=="BULLISH_GOLD": bull+=1.25; rb.append("ضغط DXY لصالح الذهب")
    elif macro=="BEARISH_GOLD": bear+=1.25; rs.append("ضغط DXY ضد الذهب")
    mw=min(1.15,max(0,(confidence-0.35)/0.65*1.15))
    if mw and bull>=bear: bull+=mw
    if mw and bear>bull: bear+=mw
    if h4 in {"BULLISH","BEARISH"} and hmm in {"BULLISH","BEARISH"} and h4!=hmm:
        if h4=="BULLISH": bull-=0.25
        else: bear-=0.25
    direction="BUY" if bull>=APPROVE_SCORE and bull-bear>=MIN_DIRECTION_MARGIN else "SELL" if bear>=APPROVE_SCORE and bear-bull>=MIN_DIRECTION_MARGIN else None
    return direction,{"bull_score":round(bull,2),"bear_score":round(bear,2),"margin":round(abs(bull-bear),2),"reasons_bull":rb,"reasons_bear":rs}

def _levels(direction,entry,atr,feed):
    try: spread=abs(float(feed.get("ask") or entry)-float(feed.get("bid") or entry))
    except Exception: spread=0.0
    dist=max(float(atr)*1.25,entry*MIN_STOP_PCT,spread*2)
    if direction=="BUY": return round(entry-dist,2),round(entry+dist*PREFERRED_RR1,2),round(entry+dist*PREFERRED_RR2,2),dist
    return round(entry+dist,2),round(entry-dist*PREFERRED_RR1,2),round(entry-dist*PREFERRED_RR2,2),dist

def _rr(direction,entry,sl,tp1):
    risk=abs(entry-sl); reward=(tp1-entry) if direction=="BUY" else (entry-tp1); return reward/risk if risk>0 else 0.0

def _generate(bot):
    data=bot.analyze_institutional_engine()
    if not data: return {"status":"WAIT","reason":"تعذر تجهيز بيانات السوق الحالية.","price":bot.get_market_data().get("gold",0.0)}
    df=data["df_m15"]; c=bot.to_1d_series(df["Close"]); h=bot.to_1d_series(df["High"]); l=bot.to_1d_series(df["Low"]); price=float(data["last_price"]); rsi=float(ta.momentum.RSIIndicator(c,window=14).rsi().iloc[-1]); atr=float(ta.volatility.AverageTrueRange(h,l,c,window=14).average_true_range().iloc[-1]); ema_f=float(ta.trend.EMAIndicator(c,window=9).ema_indicator().iloc[-1]); ema_s=float(ta.trend.EMAIndicator(c,window=21).ema_indicator().iloc[-1]); macd=float(ta.trend.MACD(c).macd_diff().iloc[-1]); stoch=float(ta.momentum.StochasticOscillator(h,l,c).stoch().iloc[-1]); vol=(atr/price)*100 if price else 0
    clf=bot.train_self_learning_model(); dxy=float(data["dxy_corr"]) if data.get("dxy_corr") is not None else 0.0; feat=pd.DataFrame([[rsi,dxy,macd,stoch,vol]],columns=["rsi","dxy_corr","macd_diff","stoch_k","volatility_ratio"]); conf=float(bot.calculate_dynamic_confidence(data["h4_trend"],data["state_label"],ema_f,ema_s,rsi,data["smc"],vol,dxy,clf,feat))
    if conf<MIN_CONFIDENCE: return {"status":"WAIT","decision_state":"WATCH","reason":f"فرصة تحت المراقبة فقط: الثقة {int(conf*100)}% أقل من {int(MIN_CONFIDENCE*100)}%.","price":price}
    data["ema_fast"]=ema_f; data["ema_slow"]=ema_s; direction,sc=_score(data,ema_f,ema_s,conf)
    if not direction: return {"status":"WAIT","decision_state":"WAIT","reason":f"لا يوجد ترجيح اتجاهي كافٍ. BUY={sc['bull_score']:.2f} | SELL={sc['bear_score']:.2f} | الثقة={int(conf*100)}%.","price":price}
    if conf<LOW_CONFIDENCE and max(sc["bull_score"],sc["bear_score"])<LOW_CONFIDENCE_SCORE: return {"status":"WAIT","decision_state":"WATCH","reason":f"فرصة مراقبة فقط: الثقة {int(conf*100)}% مع قوة اتجاهية غير كافية.","price":price}
    feed=data.get("price_feed") or {}; entry=bot.get_xauusd_execution_price(feed,direction,"ENTRY")
    if entry is None: return {"status":"WAIT","reason":f"سعر التنفيذ غير متاح: {feed.get('status','MISSING')}","price":price}
    entry=float(entry); sl,tp1,tp2,dist=_levels(direction,entry,atr,feed); rr=_rr(direction,entry,sl,tp1)
    if rr<MIN_RR: return {"status":"WAIT","reason":f"RR={rr:.2f} أقل من {MIN_RR:.2f}.","price":price}
    ok,why=bot.validate_trade_levels(direction,entry,sl,tp1,tp2)
    if not ok: return {"status":"WAIT","reason":f"مستويات Entry/SL/TP غير صالحة: {why}","price":price}
    candle_time=pd.Timestamp(df.index[-1]).isoformat(); candle_id=f"XAUUSD_M15_{pd.Timestamp(df.index[-1]).strftime('%Y%m%d_%H%M')}"; note="تأكيد صاعد من السيولة/FVG" if direction=="BUY" else "تأكيد هابط من السيولة/FVG"
    sig={"status":"SIGNAL","type":"🟢 شراء مرن" if direction=="BUY" else "🔴 بيع مرن","entry":round(entry,2),"sl":sl,"tp1":tp1,"tp2":tp2,"rr":round(rr,2),"rsi":round(rsi,1),"dxy_corr":round(dxy,3) if data.get("dxy_corr") is not None else None,"dxy_trend":data.get("dxy_trend","UNKNOWN"),"dxy_pressure":data.get("dxy_pressure","NEUTRAL"),"confidence":int(conf*100),"risk":"1% مبدئياً (تُراجع حسب الثقة والتذبذب)","smc_note":note,"candle_id":candle_id,"signal_candle_close":round(float(c.iloc[-1]),2),"signal_candle_time":candle_time,"score_bull":sc["bull_score"],"score_bear":sc["bear_score"],"direction_margin":sc["margin"],"h4_trend":data["h4_trend"],"hmm_state":data["state_label"],"stop_distance":round(dist,2)}
    ai=bot.gemini_verify_signal(sig,{"h4_trend":data["h4_trend"],"state_label":data["state_label"],"dxy_trend":data.get("dxy_trend"),"dxy_pressure":data.get("dxy_pressure"),"dxy_corr":data.get("dxy_corr"),"rr":rr}); sig["gemini_note"]=str(ai.get("reason") or "تمت المراجعة"); sig["ai_approved"]=ai.get("approved"); sig["ai_advisory"]=not bool(ai.get("approved")); sig["ai_score"]=1.0 if ai.get("approved") else 0.0; sig["final_decision"]="APPROVE_WITH_CAUTION" if not ai.get("approved") else "APPROVE"; sig["decision_state"]="TRADE_READY"; sig["final_reason"]="التحفظ من Gemini استشاري فقط؛ القرار الكمي النهائي يسمح بالدخول بحذر." if not ai.get("approved") else "اجتازت الإشارة التقييم الكمي ووافق Gemini."; return sig

def _patch_generation(bot):
    def wrapped(*args,**kwargs):
        result=_generate(bot)
        if isinstance(result,dict) and result.get("status")=="SIGNAL":
            direction="BUY" if "شراء" in result.get("type","") else "SELL"; bucket=round(float(result.get("entry",0))/max(float(result.get("entry",1))*PRICE_BUCKET_PCT,1)); key=hashlib.sha256(f"{direction}|{result.get('h4_trend')}|{result.get('hmm_state')}|{result.get('smc_note')}|{bucket}".encode()).hexdigest()[:32]; result["setup_key"]=key
            if _find_setup(bot,key): return {"status":"WAIT","decision_state":"DUPLICATE_SETUP","reason":"تم منع تكرار نفس الإعداد في نفس المنطقة السعرية؛ ننتظر تغيراً مادياً في البنية.","price":result.get("entry",0.0)}
            _remember_setup(bot,key,direction,str(result.get("candle_id") or ""),float(result.get("entry") or 0.0))
            _save_audit(bot,{"candle_id":result.get("candle_id"),"direction":direction,"final_decision":result.get("final_decision"),"confidence":result.get("confidence"),"risk_score":0,"bull_score":result.get("score_bull"),"bear_score":result.get("score_bear"),"margin":result.get("direction_margin"),"h4_trend":result.get("h4_trend"),"hmm_state":result.get("hmm_state"),"dxy_corr":result.get("dxy_corr"),"dxy_trend":result.get("dxy_trend"),"dxy_pressure":result.get("dxy_pressure"),"rr":result.get("rr"),"reason":result.get("final_reason"),"ai_approved":result.get("ai_approved")})
        return result
    wrapped._canonical_decision_layer=True; bot.generate_quant_signal=wrapped

def _arabic_map(v): return {"BULLISH":"صاعد 🟢","BEARISH":"هابط 🔴","RANGING":"متذبذب 🟡","NEUTRAL":"محايد 🟡"}.get(str(v),str(v))
def _news_action(v): return {"NEWS_BUY":"🟢 شراء فوري بناءً على الخبر","NEWS_SELL":"🔴 بيع فوري بناءً على الخبر","WAIT_CONFIRMATION":"⏳ انتظار تأكيد سعري","NO_TRADE":"🟡 لا توجد صفقة خبرية","REDUCE_RISK":"🛡️ تخفيف المخاطرة","EXIT":"🚨 خروج من الصفقة","REASSESS":"🔎 إعادة تقييم الصفقة"}.get(str(v),str(v) or "غير متوفر")

def _lawyer_snapshot(bot):
    conn=None
    try:
        conn=bot.get_db_connection(); cur=conn.cursor(); cur.execute("SELECT id,signal_type,entry_price,sl,tp1,tp2,trade_status FROM trades WHERE outcome IS NULL AND trade_status IN ('OPEN','TP1_HIT') ORDER BY id DESC LIMIT 1"); row=cur.fetchone()
    except Exception: row=None
    finally:
        if conn is not None:
            try: bot.release_db_connection(conn)
            except Exception: pass
    if not row: return "🧑‍⚖️ لا توجد صفقة نشطة حالياً تحتاج إلى مراجعة."
    tid,sig,entry,sl,tp1,tp2,status=row; price=float((bot.get_market_data() or {}).get("gold") or entry); direction="BUY" if "BUY" in str(sig).upper() or "شراء" in str(sig) else "SELL"; action="HOLD"; reason="الصفقة ما زالت ضمن منطقة الإدارة الطبيعية."
    if direction=="BUY":
        if price>=float(tp1): action="PROTECT_PROFIT"; reason="تم بلوغ TP1؛ الأفضل حماية الربح.";
        elif price<=float(sl): action="EXIT"; reason="السعر وصل إلى/تحت SL.";
        elif price < float(entry)-0.5*abs(float(entry)-float(sl)): action="REDUCE_RISK"; reason="ضغط سعري متوسط؛ نخفف المخاطرة دون خروج آلي.";
    else:
        if price<=float(tp1): action="PROTECT_PROFIT"; reason="تم بلوغ TP1؛ الأفضل حماية الربح.";
        elif price>=float(sl): action="EXIT"; reason="السعر وصل إلى/فوق SL.";
        elif price > float(entry)+0.5*abs(float(entry)-float(sl)): action="REDUCE_RISK"; reason="ضغط سعري متوسط؛ نخفف المخاطرة دون خروج آلي.";
    return f"🧑‍⚖️ محامي الصفقة\n\nالصفقة: {'شراء' if direction=='BUY' else 'بيع'} | الحالة: {status}\nالسعر الحالي: ${price:.2f}\nالدخول: ${float(entry):.2f}\nSL: ${float(sl):.2f}\nTP1: ${float(tp1):.2f}\nTP2: ${float(tp2):.2f}\n\nالحكم: {action}\nالقرار: {reason}\n\nالمحامي مستشار مرن؛ لا يوسع SL ولا يفتح صفقة عكسية تلقائياً."

async def _news_snapshot(bot):
    global _NEWS_ENGINE
    try:
        from news_intelligence import NewsIntelligence, classify_gold_impact
        if _NEWS_ENGINE is None: _NEWS_ENGINE=NewsIntelligence()
        articles=_NEWS_ENGINE.fetch_latest()
        if not articles: return "📰 لا يوجد حالياً خبر جديد مؤثر تم التقاطه."
        lines=["📰 أخبار الذهب — آخر الأخبار المؤثرة\n"]
        for a in articles[:5]:
            imp=classify_gold_impact(a); lines.append(f"• {a.title}\n  الاتجاه: {_arabic_map(imp.direction.replace('BULLISH_GOLD','BULLISH').replace('BEARISH_GOLD','BEARISH'))}\n  القرار: {_news_action('WAIT_CONFIRMATION' if imp.material else 'NO_TRADE')}\n  التأثير: {imp.impact}/100 | الثقة: {imp.confidence}% | الاستعجال: {({'HIGH':'عالية 🔴','MEDIUM':'متوسطة 🟠','LOW':'منخفضة 🟢'}.get(imp.urgency,imp.urgency))}\n  المصدر: {a.source}")
        return "\n".join(lines)
    except Exception as exc: return f"📰 تعذر تحديث أخبار الذهب حالياً: {type(exc).__name__}: {exc}"

async def _lawyer_handler(update,context):
    bot=context.application.bot; cid=update.effective_chat.id
    if not bot._decision_layer_auth(cid): await update.message.reply_text("🔒 يرجى إدخال كلمة السر أولاً."); return
    await update.message.reply_text(_lawyer_snapshot(bot),reply_markup=bot.get_main_keyboard())
async def _news_handler(update,context):
    bot=context.application.bot; cid=update.effective_chat.id
    if not bot._decision_layer_auth(cid): await update.message.reply_text("🔒 يرجى إدخال كلمة السر أولاً."); return
    await update.message.reply_text(await _news_snapshot(bot),reply_markup=bot.get_main_keyboard())
async def _button_router(update,context):
    q=update.callback_query; await q.answer(); bot=context.application.bot
    if q.data=="lawyer": await q.edit_message_text(_lawyer_snapshot(bot))
    elif q.data=="news": await q.edit_message_text(await _news_snapshot(bot))
async def _text_router(update,context):
    text=(update.message.text or "").strip()
    if text=="🧑‍⚖️ محامي الصفقة": await _lawyer_handler(update,context)
    elif text=="📰 أخبار الذهب": await _news_handler(update,context)

def _patch_ui(bot):
    orig=getattr(bot,"get_main_keyboard",None)
    if orig and not getattr(orig,"_canonical_decision_layer",False):
        def keyboard():
            from telegram import KeyboardButton,ReplyKeyboardMarkup
            base=orig(); rows=list(getattr(base,"keyboard",[]) or []); rows.append([KeyboardButton("🧑‍⚖️ محامي الصفقة"),KeyboardButton("📰 أخبار الذهب")]); return ReplyKeyboardMarkup(rows,resize_keyboard=True)
        keyboard._canonical_decision_layer=True; bot.get_main_keyboard=keyboard
    bot._decision_layer_auth=lambda cid: bool(bot.is_authenticated(cid))

def _patch_manual_signal(bot):
    async def signal(update,context):
        cid=update.effective_chat.id
        if not bot._decision_layer_auth(cid): await bot.safe_reply_text(update,"🔒 يرجى إدخال كلمة السر أولاً."); return
        sig=await __import__('asyncio').to_thread(bot.generate_quant_signal)
        if sig and sig.get("status")=="SIGNAL":
            label="⚠️ Gemini متحفظ — السماح بالدخول بحذر." if sig.get("ai_advisory") else "✅ Gemini مؤيد."
            msg=(f"🚨 إشارة كمية مؤسسية\nالنوع: {sig['type']}\nالثقة: {sig['confidence']}%\nالدخول: ${sig['entry']}\nSL: ${sig['sl']}\nTP1: ${sig['tp1']}\nTP2: ${sig['tp2']}\nRR: {sig['rr']}\nH4: {_arabic_map(sig.get('h4_trend'))}\nHMM: {_arabic_map(sig.get('hmm_state'))}\nDXY: {sig.get('dxy_trend','غير متوفر')} | ضغط: {sig.get('dxy_pressure','محايد')}\n\n{label}\nملاحظة Gemini: {sig.get('gemini_note','')}\n\nالحكم النهائي: {sig.get('final_decision','APPROVE')}\n{sig.get('final_reason','')}")
        else: msg=f"⏸️ تنبيه الانتظار\nالسبب: {sig.get('reason','لا توجد فرصة مطابقة') if sig else 'لا توجد فرصة'}"
        await bot.safe_reply_text(update,msg,reply_markup=bot.get_main_keyboard())
    signal._canonical_decision_layer=True; bot.signal=signal

def _patch_stats(bot):
    async def stats(update,context):
        cid=update.effective_chat.id
        if not bot._decision_layer_auth(cid): await bot.safe_reply_text(update,"🔒 يرجى إدخال كلمة السر أولاً."); return
        conn=None
        try:
            conn=bot.get_db_connection(); cur=conn.cursor(); cur.execute("SELECT COUNT(*),SUM(CASE WHEN final_decision IN ('APPROVE','APPROVE_WITH_CAUTION') THEN 1 ELSE 0 END) FROM decision_audit"); total,ok=cur.fetchone(); total=total or 0; ok=ok or 0; cur.execute("SELECT direction,COUNT(*) FROM decision_audit GROUP BY direction"); cand=dict(cur.fetchall()); cur.execute("SELECT direction,COUNT(*) FROM decision_audit WHERE final_decision IN ('APPROVE','APPROVE_WITH_CAUTION') GROUP BY direction"); approved=dict(cur.fetchall()); msg=f"📊 تقرير القرار\nالمرشحون: {total}\nالمقبولون: {ok}\nBUY: {cand.get('BUY',0)} مرشح / {approved.get('BUY',0)} مقبول\nSELL: {cand.get('SELL',0)} مرشح / {approved.get('SELL',0)} مقبول\n\nالإحصائية تفصل بين المرشح والإشارة النهائية والصفقات المسجلة."
        except Exception as exc: msg=f"⚠️ تعذر قراءة تقرير القرار: {exc}"
        finally:
            if conn is not None:
                try: bot.release_db_connection(conn)
                except Exception: pass
        await bot.safe_reply_text(update,msg,reply_markup=bot.get_main_keyboard())
    stats._canonical_decision_layer=True; bot.stats=stats

def _patch_post_init(bot):
    orig=getattr(bot,"post_init",None)
    if orig and not getattr(orig,"_canonical_decision_layer",False):
        async def post_init(app):
            try:
                from telegram.ext import CommandHandler,MessageHandler,CallbackQueryHandler,filters
                app.add_handler(CommandHandler("lawyer",_lawyer_handler),group=-20); app.add_handler(CommandHandler("news",_news_handler),group=-20); app.add_handler(CallbackQueryHandler(_button_router,pattern=r"^(lawyer|news)$"),group=-20); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,_text_router),group=-20); app._decision_layer_ready=True
            except Exception as exc: LOGGER.exception("[UI] %s",exc)
            await orig(app)
        post_init._canonical_decision_layer=True; bot.post_init=post_init

def _patch_scanner(bot):
    async def scanner(app):
        last_candle=None; last_lawyer=""; last_alert=0.0
        while True:
            try:
                await __import__('asyncio').to_thread(bot.monitor_open_trades); sig=await __import__('asyncio').to_thread(bot.generate_quant_signal)
                if sig and sig.get("status")=="SIGNAL" and sig.get("candle_id")!=last_candle:
                    last_candle=sig.get("candle_id"); label="⚠️ Gemini متحفظ — القرار النهائي: السماح بحذر." if sig.get("ai_advisory") else "✅ Gemini مؤيد."; msg=(f"🚨 **إشارة تداول جديدة**\n───────────────────\nالنوع: {sig['type']}\nالثقة: {sig['confidence']}%\nالدخول: ${sig['entry']}\nSL: ${sig['sl']}\nTP1: ${sig['tp1']}\nTP2: ${sig['tp2']}\nRR: {sig['rr']}\nH4: {_arabic_map(sig.get('h4_trend'))}\nHMM: {_arabic_map(sig.get('hmm_state'))}\nDXY: {sig.get('dxy_trend','غير متوفر')} | ضغط: {sig.get('dxy_pressure','محايد')}\nSMC: {sig.get('smc_note','')}\n\n{label}\nملاحظة Gemini: {sig.get('gemini_note','')}\n\n**الحكم النهائي:** {sig.get('final_decision','APPROVE')}\n{sig.get('final_reason','')}")
                    for uid in bot.get_subscribers():
                        if bot.is_authenticated(uid):
                            try: await bot.safe_send_message(app.bot,chat_id=uid,text=msg,parse_mode='Markdown',reply_markup=bot.get_main_keyboard())
                            except Exception: pass
                advice=_lawyer_snapshot(bot); action=advice.split("الحكم:")[-1].split("\n")[0].strip() if "الحكم:" in advice else "HOLD"; now=time.monotonic()
                if "لا توجد صفقة نشطة" not in advice and action!=last_lawyer and now-last_alert>=LAWYER_COOLDOWN_SECONDS:
                    last_lawyer=action; last_alert=now
                    for uid in bot.get_subscribers():
                        if bot.is_authenticated(uid):
                            try: await bot.safe_send_message(app.bot,chat_id=uid,text=advice,reply_markup=bot.get_main_keyboard())
                            except Exception: pass
            except Exception as exc: LOGGER.warning("[SCANNER] %s",exc)
            await __import__('asyncio').sleep(30)
    scanner._canonical_decision_layer=True; bot.auto_market_scanner=scanner

def _install(bot):
    global _PATCHED
    deadline=time.monotonic()+120; required=("analyze_institutional_engine","generate_quant_signal","post_init","auto_market_scanner","get_main_keyboard","stats","signal")
    while time.monotonic()<deadline:
        if all(hasattr(bot,n) for n in required):
            _ensure_schema(bot); _patch_analysis(bot); _patch_generation(bot); _patch_ui(bot); _patch_manual_signal(bot); _patch_stats(bot); _patch_post_init(bot); _patch_scanner(bot); _PATCHED=True; LOGGER.info("✅ Canonical decision layer installed."); return
        time.sleep(.25)
    LOGGER.error("❌ Canonical decision layer timed out.")

def install(bot):
    global _INSTALLED
    with _LOCK:
        if _INSTALLED: return
        _INSTALLED=True
    threading.Thread(target=_install,args=(bot,),name="canonical-decision-layer",daemon=True).start()

def health(): return {"installed":_INSTALLED,"patched":_PATCHED,"dxy_symbol":_DXY_STATE.get("symbol"),"dxy_error":_DXY_STATE.get("error"),"news_engine":_NEWS_ENGINE is not None}

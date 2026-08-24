"""Near-real-time news runtime for XAU/USD.

Keeps news separate from the Twelve Data gold price channel while anchoring
reaction measurement to article publication time, clustering duplicate reports,
and feeding only confirmed material events into the decision/lawyer context.
"""
from __future__ import annotations
import hashlib, logging, threading, time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

LOGGER=logging.getLogger("XAUUSD_QuantBot.NewsRuntime")
_LOCK=threading.RLock(); _INSTALLED=False; _THREAD=None
_PRICE_HISTORY=deque(maxlen=5000); _EVENTS={}; _LAST_POLL=0.0
POLL_SECONDS=float(__import__('os').getenv("NEWS_RUNTIME_POLL_SECONDS","60")); REACTION_WINDOW=float(__import__('os').getenv("NEWS_REACTION_WINDOW_SECONDS","120")); MIN_MOVE=float(__import__('os').getenv("NEWS_MIN_REACTION_PCT","0.20")); CLUSTER_SECONDS=float(__import__('os').getenv("NEWS_EVENT_CLUSTER_SECONDS","300"))

def _now(): return datetime.now(timezone.utc)
def _price(bot):
    try:
        market=bot.get_market_data() or {}; feed=market.get("price_feed") or {}; v=feed.get("mid") or feed.get("spot") or market.get("gold"); return float(v) if v else None
    except Exception: return None

def sample_price(bot):
    p=_price(bot)
    if p and p>0:
        _PRICE_HISTORY.append((_now(),p))
    cutoff=_now().timestamp()-max(900,REACTION_WINDOW*5)
    while _PRICE_HISTORY and _PRICE_HISTORY[0][0].timestamp()<cutoff: _PRICE_HISTORY.popleft()

def _event_key(article):
    title=" ".join(str(article.title or "").lower().split())
    bucket=int(article.published_at.timestamp()//CLUSTER_SECONDS)
    return hashlib.sha256(f"{title}|{bucket}".encode()).hexdigest()[:20]

def reaction_for(article):
    points=[(ts,p) for ts,p in _PRICE_HISTORY if ts>=article.published_at and (ts-article.published_at).total_seconds()<=REACTION_WINDOW]
    if not points: return {"confirmed":False,"change_pct":0.0,"direction":"UNKNOWN","baseline":None}
    baseline=points[0][1]; current=points[-1][1] if points else baseline
    change=(current-baseline)/baseline*100 if baseline else 0.0
    return {"confirmed":abs(change)>=MIN_MOVE,"change_pct":round(change,4),"direction":"UP" if change>0 else "DOWN" if change<0 else "FLAT","baseline":baseline}

def _ensure_cache(bot):
    cache=getattr(bot,"GLOBAL_CACHE",None)
    if isinstance(cache,dict):
        cache.setdefault("news_health",{})
        cache.setdefault("news_events",{})

def poll_once(bot):
    global _LAST_POLL
    from news_intelligence import NewsIntelligence, classify_gold_impact
    global _engine
    try:
        engine=globals().get("_engine")
        if engine is None:
            engine=NewsIntelligence(); globals()["_engine"]=engine
        articles=engine.fetch_latest(); _LAST_POLL=time.time(); _ensure_cache(bot); cache=bot.GLOBAL_CACHE
        clusters={}
        material=[]
        for article in articles:
            key=_event_key(article); clusters[key]=clusters.get(key,0)+1; impact=classify_gold_impact(article); reaction=reaction_for(article)
            rec={"event_id":key,"title":article.title,"source":article.source,"published_at":article.published_at.isoformat(),"direction":impact.direction,"impact":impact.impact,"confidence":impact.confidence,"urgency":impact.urgency,"reaction":reaction,"sources":clusters[key]}
            _EVENTS[key]=rec
            if impact.material and reaction["confirmed"]: material.append(rec)
        cache["latest_news"]=[_EVENTS[k] for k in list(_EVENTS.keys())[-20:]]
        cache["news_health"]={"last_success":_now().isoformat(),"poll_seconds":POLL_SECONDS,"articles":len(articles),"event_clusters":len(clusters),"confirmed_material":len(material),"reaction_window_seconds":REACTION_WINDOW,"near_real_time":True}
        if material:
            material.sort(key=lambda x:(x["impact"],x["confidence"]),reverse=True); cache["news_candidate"]=material[0]
        return articles
    except Exception as exc:
        LOGGER.warning("[NEWS_RUNTIME] poll failed: %s",exc)
        cache=getattr(bot,"GLOBAL_CACHE",None)
        if isinstance(cache,dict): cache["news_health"]={"last_error":str(exc),"last_poll":_now().isoformat(),"poll_seconds":POLL_SECONDS}
        return []

def _worker(bot):
    while True:
        try:
            sample_price(bot)
            if time.time()-_LAST_POLL>=POLL_SECONDS: poll_once(bot)
        except Exception as exc: LOGGER.debug("[NEWS_RUNTIME] %s",exc)
        time.sleep(2.0)

def start(bot):
    global _INSTALLED,_THREAD
    with _LOCK:
        if _INSTALLED and _THREAD and _THREAD.is_alive(): return
        _INSTALLED=True; _THREAD=threading.Thread(target=_worker,args=(bot,),name="gold-news-runtime",daemon=True); _THREAD.start(); LOGGER.info("✅ News runtime started: timestamp attribution + event clustering + price confirmation")

def health():
    return {"installed":_INSTALLED,"last_poll":_LAST_POLL,"events":len(_EVENTS),"price_samples":len(_PRICE_HISTORY)}

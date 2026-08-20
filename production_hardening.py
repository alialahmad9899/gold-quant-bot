"""Production hardening layer for the XAU/USD decision pipeline."""
from __future__ import annotations
import hashlib, logging, os, re, threading, time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger("XAUUSD_QuantBot.ProductionHardening")
_INSTALL_LOCK = threading.Lock(); _INSTALLED = False; _NEWS_THREAD: threading.Thread | None = None
HARD_GEMINI_VETO = os.getenv("SIGNAL_SAFETY_GEMINI_HARD_VETO", "0") == "1"
NEWS_REACTION_WINDOW_SECONDS = float(os.getenv("NEWS_REACTION_WINDOW_SECONDS", "120")); NEWS_MIN_REACTION_PCT = float(os.getenv("NEWS_MIN_REACTION_PCT", "0.20")); NEWS_EVENT_CLUSTER_SECONDS = float(os.getenv("NEWS_EVENT_CLUSTER_SECONDS", "300")); DIRECTION_WINDOW = int(os.getenv("DIRECTION_CALIBRATION_WINDOW", "100")); PRICE_SAMPLE_SECONDS = float(os.getenv("NEWS_PRICE_SAMPLE_SECONDS", "2"))


def _now() -> datetime: return datetime.now(timezone.utc)
def _norm(text: Any) -> str: return re.sub(r"\s+", " ", str(text or "").strip()).lower()
def _event_key(title: str, published_at: datetime) -> str:
    tokens = re.findall(r"[a-z0-9]+", _norm(title))[:18]; bucket = int(published_at.timestamp() // NEWS_EVENT_CLUSTER_SECONDS); return hashlib.sha256((" ".join(tokens) + f"|{bucket}").encode()).hexdigest()[:16]

def parse_economic_surprise(text: str) -> dict[str, float | None]:
    text = str(text or ""); patterns = {"actual": r"(?:actual|reported|released)\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*%?", "forecast": r"(?:forecast|expected|estimate)\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*%?", "previous": r"(?:previous|prior)\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*%?"}; out: dict[str, float | None] = {k: None for k in patterns}
    for key, pattern in patterns.items():
        m = re.search(pattern, text, re.I)
        if m:
            try: out[key] = float(m.group(1))
            except ValueError: pass
    out["surprise"] = out["actual"] - out["forecast"] if out["actual"] is not None and out["forecast"] is not None else None; return out

def semantic_news_signal(title: str, summary: str = "") -> dict[str, Any]:
    text = _norm(f"{title} {summary}"); positive = ("rate cut", "cuts rates", "dovish", "easing", "lower rates", "weaker dollar", "dollar falls", "falling yields", "safe haven", "gold rises", "gold gains", "inflation rises", "war escalates", "escalation", "sanction"); negative = ("rate hike", "hikes rates", "hawkish", "higher rates", "strong dollar", "dollar rises", "rising yields", "yield rises", "gold falls", "gold drops", "inflation cools", "ceasefire", "dovish expectations fade"); bull = sum(1 for p in positive if p in text); bear = sum(1 for p in negative if p in text); surprise = parse_economic_surprise(text)
    if surprise.get("surprise") is not None:
        macro = any(k in text for k in ("cpi", "inflation", "nfp", "payroll", "jobs", "employment", "pce")); s = float(surprise["surprise"] or 0); bear += 2 if macro and s > 0 else 0; bull += 2 if macro and s < 0 else 0
    direction = "NEUTRAL" if bull == bear else "BULLISH_GOLD" if bull > bear else "BEARISH_GOLD"; direct = any(k in text for k in ("gold", "xau", "bullion")); high = any(k in text for k in ("fomc", "fed", "powell", "cpi", "pce", "nfp", "payroll", "rate decision", "emergency")); impact = min(100, 25 + 20 * int(direct) + 15 * int(high) + 15 * min(3, abs(bull - bear))); impact = min(impact, 30) if direction == "NEUTRAL" else impact; confidence = min(95, 45 + 10 * int(direct) + 10 * int(high) + 10 * min(3, abs(bull - bear)) + 10 * int(surprise.get("surprise") is not None)); return {"direction": direction, "impact": impact, "confidence": confidence, "surprise": surprise, "material": bool(direction != "NEUTRAL" and impact >= 45)}

def price_reaction_from_event(event_time: datetime, now_price: float, price_history: list[tuple[datetime, float]]) -> dict[str, Any]:
    event_time = event_time if event_time.tzinfo else event_time.replace(tzinfo=timezone.utc); candidates = [(ts, float(px)) for ts, px in price_history if ts >= event_time and (ts - event_time).total_seconds() <= NEWS_REACTION_WINDOW_SECONDS]; baseline = candidates[0][1] if candidates else None
    if baseline is None or baseline <= 0 or now_price <= 0: return {"confirmed": False, "change_pct": 0.0, "direction": "UNKNOWN", "baseline": baseline}
    change_pct = (now_price - baseline) / baseline * 100.0; direction = "UP" if change_pct > 0 else "DOWN" if change_pct < 0 else "FLAT"; return {"confirmed": abs(change_pct) >= NEWS_MIN_REACTION_PCT, "change_pct": round(change_pct, 4), "direction": direction, "baseline": baseline}

def _is_pg(bot: Any, conn: Any) -> bool:
    try: return bool(getattr(bot, "is_postgres", lambda: False)())
    except Exception: return False

def _ensure_tables(bot: Any) -> None:
    conn = None
    try:
        conn = bot.get_db_connection(); cur = conn.cursor(); pg = _is_pg(bot, conn)
        statements = ["CREATE TABLE IF NOT EXISTS direction_calibration (id BIGSERIAL PRIMARY KEY, direction TEXT NOT NULL, outcome TEXT NOT NULL, candle_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)", "CREATE INDEX IF NOT EXISTS idx_direction_calibration_created ON direction_calibration(created_at)", "CREATE TABLE IF NOT EXISTS news_events (event_id TEXT PRIMARY KEY, title TEXT, source TEXT, published_at TEXT, direction TEXT, impact INTEGER, confidence INTEGER, surprise DOUBLE PRECISION, source_count INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)", "CREATE INDEX IF NOT EXISTS idx_news_events_published ON news_events(published_at)"] if pg else ["CREATE TABLE IF NOT EXISTS direction_calibration (id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT NOT NULL, outcome TEXT NOT NULL, candle_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)", "CREATE INDEX IF NOT EXISTS idx_direction_calibration_created ON direction_calibration(created_at)", "CREATE TABLE IF NOT EXISTS news_events (event_id TEXT PRIMARY KEY, title TEXT, source TEXT, published_at TEXT, direction TEXT, impact INTEGER, confidence INTEGER, surprise REAL, source_count INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP)", "CREATE INDEX IF NOT EXISTS idx_news_events_published ON news_events(published_at)"]
        for sql in statements: cur.execute(sql)
        conn.commit()
    except Exception as exc:
        LOGGER.debug("hardening DB schema not ready: %s", exc)
        try:
            if conn: conn.rollback()
        except Exception: pass
    finally:
        if conn is not None:
            try: bot.release_db_connection(conn)
            except Exception:
                try: conn.close()
                except Exception: pass

def record_direction(bot: Any, direction: str, outcome: str, candle_id: str = "") -> None:
    direction = str(direction or "").upper()
    if direction not in {"BUY", "SELL"}: return
    cache = getattr(bot, "GLOBAL_CACHE", None); history = list(cache.get("direction_calibration") or []) if isinstance(cache, dict) else []; history.append({"direction": direction, "outcome": outcome, "candle_id": candle_id, "timestamp": _now().isoformat()}); history = history[-DIRECTION_WINDOW:]
    if isinstance(cache, dict):
        cache["direction_calibration"] = history; total = len(history); buys = sum(x["direction"] == "BUY" for x in history); sells = total - buys; cache["direction_balance"] = {"window": total, "buy_pct": round(100 * buys / total, 1) if total else 0.0, "sell_pct": round(100 * sells / total, 1) if total else 0.0, "warning": total >= 20 and (buys / total < 0.20 or buys / total > 0.80)}
    conn = None
    try:
        conn = bot.get_db_connection(); cur = conn.cursor(); pg = _is_pg(bot, conn); sql = "INSERT INTO direction_calibration(direction,outcome,candle_id) VALUES (%s,%s,%s)" if pg else "INSERT INTO direction_calibration(direction,outcome,candle_id) VALUES (?,?,?)"; cur.execute(sql, (direction, outcome, candle_id)); conn.commit()
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

def _patch_signal_safety() -> bool:
    try:
        import signal_safety
        if HARD_GEMINI_VETO: return True
        original = signal_safety._normalize_vetting_result
        if getattr(original, "_production_hardening", False): return True
        def advisory(raw: Any) -> dict[str, Any]:
            result = original(raw)
            if not result.get("approved", False): result["approved"] = True; result["advisory"] = True; result["reason"] = f"مراجعة Gemini تحفظية فقط: {result.get('reason') or 'لا توجد موافقة AI'}"
            return result
        advisory._production_hardening = True; signal_safety._normalize_vetting_result = advisory; LOGGER.info("🧠 Gemini signal veto switched to advisory mode; institutional hard veto remains canonical."); return True
    except Exception as exc: LOGGER.debug("signal-safety hardening deferred: %s", exc); return False

def _patch_news_module(bot: Any) -> bool:
    try:
        import news_intelligence as ni
        original_classify = ni.classify_gold_impact
        if not getattr(original_classify, "_production_hardening", False):
            def classify(article: Any):
                semantic = semantic_news_signal(getattr(article, "title", ""), getattr(article, "summary", "")); base = original_classify(article); return ni.NewsImpact(semantic["direction"], int(max(base.impact, semantic["impact"])), int(max(base.confidence, semantic["confidence"])), "HIGH" if semantic["impact"] >= 70 else "MEDIUM" if semantic["impact"] >= 45 else "LOW", list(base.reasons) + ([f"surprise={semantic['surprise']['surprise']}" ] if semantic["surprise"].get("surprise") is not None else []), bool(semantic["material"]))
            classify._production_hardening = True; ni.classify_gold_impact = classify
        original_fetch = ni.NewsIntelligence.fetch_latest
        if not getattr(original_fetch, "_production_hardening", False):
            def fetch_latest(self):
                fresh = original_fetch(self); bot_cache = getattr(bot, "GLOBAL_CACHE", None)
                if isinstance(bot_cache, dict):
                    latest = []; clusters: dict[str, int] = {}
                    for article in fresh:
                        eid = _event_key(article.title, article.published_at); clusters[eid] = clusters.get(eid, 0) + 1; semantic = semantic_news_signal(article.title, article.summary); latest.append({"title": article.title, "url": article.url, "source": article.source, "published_at": article.published_at.isoformat(), "event_id": eid, "direction": semantic["direction"], "impact": semantic["impact"], "confidence": semantic["confidence"], "surprise": semantic["surprise"]})
                        conn = None
                        try:
                            conn = bot.get_db_connection(); cur = conn.cursor(); pg = _is_pg(bot, conn); sql = "INSERT INTO news_events(event_id,title,source,published_at,direction,impact,confidence,surprise,source_count) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (event_id) DO UPDATE SET source_count=news_events.source_count+1" if pg else "INSERT INTO news_events(event_id,title,source,published_at,direction,impact,confidence,surprise,source_count) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET source_count=news_events.source_count+1"; cur.execute(sql, (eid, article.title, article.source, article.published_at.isoformat(), semantic["direction"], semantic["impact"], semantic["confidence"], semantic["surprise"]["surprise"], 1)); conn.commit()
                        except Exception: pass
                        finally:
                            if conn is not None:
                                try: bot.release_db_connection(conn)
                                except Exception:
                                    try: conn.close()
                                    except Exception: pass
                    bot_cache["latest_news"] = latest[:20]; bot_cache["news_health"] = {"last_success": _now().isoformat(), "articles": len(fresh), "event_clusters": len(clusters), "feeds": list(self.feeds), "gdelt_enabled": bool(self.gdelt_enabled), "poll_seconds": self.poll_seconds, "near_real_time": True, "reaction_window_seconds": NEWS_REACTION_WINDOW_SECONDS}
                return fresh
            fetch_latest._production_hardening = True; ni.NewsIntelligence.fetch_latest = fetch_latest
        return True
    except Exception as exc: LOGGER.debug("news module hardening deferred: %s", exc); return False

def _price(bot: Any) -> float | None:
    try:
        market = bot.get_market_data() or {}; feed = market.get("price_feed") or {}; value = feed.get("mid") or feed.get("spot") or market.get("gold"); return float(value) if value is not None else None
    except Exception: return None

def _sample_prices(bot: Any, history: deque[tuple[datetime, float]]) -> None:
    while True:
        try:
            price = _price(bot)
            if price and price > 0: history.append((_now(), price))
            cutoff = _now().timestamp() - max(600, NEWS_REACTION_WINDOW_SECONDS * 4)
            while history and history[0][0].timestamp() < cutoff: history.popleft()
        except Exception: pass
        time.sleep(max(0.5, PRICE_SAMPLE_SECONDS))

def _patch_trade_lawyer(bot: Any) -> bool:
    try:
        import trade_lawyer
        history: deque[tuple[datetime, float]] = deque(maxlen=2000); threading.Thread(target=_sample_prices, args=(bot, history), name="news-price-reaction-sampler", daemon=True).start()
        news_engine = getattr(trade_lawyer, "_news", None)
        if news_engine is None: return False
        def anchored_context(bot_module: Any, active_direction: str | None = None):
            try:
                articles = news_engine.fetch_latest()
                price = _price(bot_module)
                if not articles or price is None: return None
                cache = getattr(bot_module, "GLOBAL_CACHE", None); decisions = []
                for article in articles:
                    reaction = price_reaction_from_event(article.published_at, price, list(history)); pdir = reaction["direction"]
                    if not reaction["confirmed"]: continue
                    decision = news_engine.evaluate_active_trade(active_direction, article, reaction["change_pct"], pdir) if active_direction else news_engine.evaluate_news_entry(article, reaction["change_pct"], pdir)
                    if decision.action not in {"NO_TRADE", "REASSESS", "WAIT_CONFIRMATION"}: decisions.append(decision)
                if not decisions: return None
                decision = max(decisions, key=lambda x: (x.impact, x.confidence));
                if isinstance(cache, dict): cache["news_decision"] = asdict(decision); cache["news_reference_price"] = price; cache["news_reaction"] = {"window_seconds": NEWS_REACTION_WINDOW_SECONDS, "confirmed": True}
                return decision
            except Exception as exc:
                LOGGER.debug("anchored news context failed: %s", exc); return None
        trade_lawyer._news_context = anchored_context
        import phase2_runtime_integration
        phase2_runtime_integration.get_live_news_context = anchored_context
        return True
    except Exception as exc: LOGGER.debug("trade lawyer news patch deferred: %s", exc); return False

def install(bot: Any) -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED: return
        _ensure_tables(bot); signal_ok = _patch_signal_safety(); news_ok = _patch_news_module(bot); lawyer_ok = _patch_trade_lawyer(bot)
        cache = getattr(bot, "GLOBAL_CACHE", None)
        if isinstance(cache, dict): cache.setdefault("production_hardening", {}); cache["production_hardening"].update({"installed": True, "installed_at": _now().isoformat(), "gemini_hard_veto": HARD_GEMINI_VETO, "news_reaction_window_seconds": NEWS_REACTION_WINDOW_SECONDS, "direction_window": DIRECTION_WINDOW, "signal_safety_ready": signal_ok, "news_ready": news_ok, "lawyer_news_ready": lawyer_ok})
        _INSTALLED = True; LOGGER.info("✅ Production hardening installed: signal=%s news=%s lawyer_news=%s", signal_ok, news_ok, lawyer_ok)

def start(bot: Any) -> threading.Thread:
    def worker() -> None:
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            try: install(bot); return
            except Exception as exc: LOGGER.debug("production hardening retry: %s", exc)
            time.sleep(0.5)
        LOGGER.error("❌ Production hardening installation timed out.")
    thread = threading.Thread(target=worker, name="production-hardening-bootstrap", daemon=True); thread.start(); return thread

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot.py"
text = BOT.read_text(encoding="utf-8")

old_quota = re.compile(r"# أزمنة تنظيم الطلبات للحفاظ المضمون على الخطة المجانية لـ Twelve Data\n.*?TWELVE_DATA_LOCK = threading\.Lock\(\)\n", re.S)
new_quota = '''# Twelve Data centralized budget: 750 background + 50 manual reserve, hard 800 total ceiling.
TWELVE_DATA_TOTAL_BUDGET = int(os.getenv("TWELVE_DATA_TOTAL_BUDGET", "800"))
TWELVE_DATA_BACKGROUND_BUDGET = int(os.getenv("TWELVE_DATA_BACKGROUND_BUDGET", "750"))
TWELVE_DATA_MANUAL_RESERVE = int(os.getenv("TWELVE_DATA_MANUAL_RESERVE", "50"))
TWELVE_DATA_MINUTE_BUDGET = int(os.getenv("TWELVE_DATA_MINUTE_BUDGET", "4"))
TWELVE_DATA_LIVE_INTERVAL = float(os.getenv("TWELVE_DATA_LIVE_INTERVAL", "180.0"))
TWELVE_DATA_OHLC_INTERVAL = float(os.getenv("TWELVE_DATA_OHLC_INTERVAL", "300.0"))
PRICE_FEED_EXECUTION_MAX_AGE = int(os.getenv("PRICE_FEED_EXECUTION_MAX_AGE", "300"))
LAST_TWELVE_DATA_QUOTE_TIME = 0.0
TWELVE_DATA_OHLC_LAST_REQUESTS = {}
TWELVE_DATA_LOCK = threading.RLock()
TWELVE_DATA_STATE = {
    "day": datetime.now(timezone.utc).date().isoformat(),
    "daily_requests": 0,
    "minute_requests": [],
    "blocked_until": 0.0,
    "backoff_seconds": 0.0,
    "daily_exhausted": False,
    "last_error": None,
    "last_request_at": None,
}
TWELVE_DATA_PERSIST_KEY = "twelve_data_quota"


def _reset_twelve_data_budget_for_tests():
    with TWELVE_DATA_LOCK:
        TWELVE_DATA_STATE.update({
            "day": datetime.now(timezone.utc).date().isoformat(),
            "daily_requests": 0,
            "minute_requests": [],
            "blocked_until": 0.0,
            "backoff_seconds": 0.0,
            "daily_exhausted": False,
            "last_error": None,
            "last_request_at": None,
        })


def _load_twelve_data_persisted_state():
    if TWELVE_DATA_STATE.get("daily_requests", 0) > 0:
        return
    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM config WHERE key = %s" if is_postgres() and isinstance(conn, psycopg2.extensions.connection) else "SELECT value FROM config WHERE key = ?", (TWELVE_DATA_PERSIST_KEY,))
            row = cur.fetchone()
        finally:
            release_db_connection(conn)
        if not row or not row[0]:
            return
        persisted = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if persisted.get("day") == datetime.now(timezone.utc).date().isoformat():
            TWELVE_DATA_STATE.update({
                "day": persisted.get("day"),
                "daily_requests": int(persisted.get("daily_requests", 0)),
                "daily_exhausted": bool(persisted.get("daily_exhausted", False)),
                "last_error": persisted.get("last_error"),
                "last_request_at": persisted.get("last_request_at"),
            })
    except Exception as exc:
        logger.warning("[TWELVE_DATA] persisted quota state unavailable: %s", exc)


def _persist_twelve_data_state():
    try:
        payload = json.dumps({
            "day": TWELVE_DATA_STATE["day"],
            "daily_requests": int(TWELVE_DATA_STATE["daily_requests"]),
            "daily_exhausted": bool(TWELVE_DATA_STATE["daily_exhausted"]),
            "last_error": TWELVE_DATA_STATE.get("last_error"),
            "last_request_at": TWELVE_DATA_STATE.get("last_request_at"),
        }, ensure_ascii=False)
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
                cur.execute("INSERT INTO config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (TWELVE_DATA_PERSIST_KEY, payload))
            else:
                cur.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (TWELVE_DATA_PERSIST_KEY, payload))
            conn.commit()
        finally:
            release_db_connection(conn)
    except Exception as exc:
        logger.warning("[TWELVE_DATA] quota state persistence failed: %s", exc)


def _refresh_twelve_data_day():
    day = datetime.now(timezone.utc).date().isoformat()
    if TWELVE_DATA_STATE.get("day") != day:
        TWELVE_DATA_STATE.update({
            "day": day,
            "daily_requests": 0,
            "minute_requests": [],
            "blocked_until": 0.0,
            "backoff_seconds": 0.0,
            "daily_exhausted": False,
            "last_error": None,
            "last_request_at": None,
        })
        _persist_twelve_data_state()


def _twelve_data_request_allowed(request_kind="quote", request_class="background"):
    request_class = "manual" if str(request_class).lower() == "manual" else "background"
    with TWELVE_DATA_LOCK:
        _refresh_twelve_data_day()
        _load_twelve_data_persisted_state()
        now = time.monotonic()
        TWELVE_DATA_STATE["minute_requests"] = [ts for ts in TWELVE_DATA_STATE["minute_requests"] if now - ts < 60.0]
        if now < float(TWELVE_DATA_STATE.get("blocked_until", 0.0) or 0.0):
            return False
        if len(TWELVE_DATA_STATE["minute_requests"]) >= TWELVE_DATA_MINUTE_BUDGET:
            return False
        used = int(TWELVE_DATA_STATE.get("daily_requests", 0))
        if used >= TWELVE_DATA_TOTAL_BUDGET:
            TWELVE_DATA_STATE["daily_exhausted"] = True
            return False
        if request_class == "background" and used >= TWELVE_DATA_BACKGROUND_BUDGET:
            return False
        return True


def _record_twelve_data_credit(request_class="background"):
    request_class = "manual" if str(request_class).lower() == "manual" else "background"
    with TWELVE_DATA_LOCK:
        _refresh_twelve_data_day()
        now = time.monotonic()
        TWELVE_DATA_STATE["daily_requests"] += 1
        TWELVE_DATA_STATE["minute_requests"] = [ts for ts in TWELVE_DATA_STATE["minute_requests"] if now - ts < 60.0]
        TWELVE_DATA_STATE["minute_requests"].append(now)
        TWELVE_DATA_STATE["last_request_at"] = datetime.now(timezone.utc).isoformat()
        TWELVE_DATA_STATE["daily_exhausted"] = TWELVE_DATA_STATE["daily_requests"] >= TWELVE_DATA_TOTAL_BUDGET
        _persist_twelve_data_state()


def _record_twelve_data_success():
    with TWELVE_DATA_LOCK:
        TWELVE_DATA_STATE["backoff_seconds"] = 0.0
        TWELVE_DATA_STATE["blocked_until"] = 0.0
        TWELVE_DATA_STATE["last_error"] = None


def _record_twelve_data_failure(error_message):
    now = time.monotonic()
    message = str(error_message or "")[:300]
    lowered = message.lower()
    with TWELVE_DATA_LOCK:
        previous = float(TWELVE_DATA_STATE.get("backoff_seconds", 0.0) or 0.0)
        base_delay = 90.0 if "429" in lowered else (60.0 if "403" in lowered or "401" in lowered else 30.0)
        delay = min(900.0, max(base_delay, previous * 2.0 if previous else base_delay))
        TWELVE_DATA_STATE["backoff_seconds"] = delay
        TWELVE_DATA_STATE["blocked_until"] = now + delay
        TWELVE_DATA_STATE["last_error"] = message
        if "daily" in lowered or "per day" in lowered or "credits exhausted" in lowered:
            TWELVE_DATA_STATE["daily_exhausted"] = True
        _persist_twelve_data_state()


def twelve_data_quota_summary():
    with TWELVE_DATA_LOCK:
        _refresh_twelve_data_day()
        _load_twelve_data_persisted_state()
        now = time.monotonic()
        minute_count = len([ts for ts in TWELVE_DATA_STATE["minute_requests"] if now - ts < 60.0])
        used = int(TWELVE_DATA_STATE["daily_requests"])
        return {
            "daily_used": used,
            "daily_total_budget": int(TWELVE_DATA_TOTAL_BUDGET),
            "background_budget": int(TWELVE_DATA_BACKGROUND_BUDGET),
            "manual_reserve": int(TWELVE_DATA_MANUAL_RESERVE),
            "daily_remaining": max(0, int(TWELVE_DATA_TOTAL_BUDGET) - used),
            "background_remaining": max(0, int(TWELVE_DATA_BACKGROUND_BUDGET) - used),
            "minute_used": int(minute_count),
            "minute_budget": int(TWELVE_DATA_MINUTE_BUDGET),
            "blocked_until": float(TWELVE_DATA_STATE.get("blocked_until", 0.0) or 0.0),
            "daily_exhausted": bool(TWELVE_DATA_STATE.get("daily_exhausted")),
            "last_error": TWELVE_DATA_STATE.get("last_error"),
        }
'''
text, n = old_quota.subn(new_quota, text, count=1)
if n != 1:
    raise SystemExit("quota configuration block not found")

quote_pattern = re.compile(r"def fetch_twelve_data_live_quote\(\):.*?(?=def fetch_twelve_data_ohlc)", re.S)
quote_replacement = '''def fetch_twelve_data_live_quote(request_class="background"):
    """Fetch real-time XAU/USD spot quote through the centralized Twelve Data budget gateway."""
    global LAST_TWELVE_DATA_QUOTE_TIME
    if not TWELVE_DATA_API_KEY:
        return None
    now = time.monotonic()
    with TWELVE_DATA_LOCK:
        last = float(LAST_TWELVE_DATA_QUOTE_TIME or 0.0)
        if last and now - last < TWELVE_DATA_LIVE_INTERVAL:
            return None
    if not _twelve_data_request_allowed("quote", request_class=request_class):
        return None
    _record_twelve_data_credit(request_class=request_class)
    url = f"https://api.twelvedata.com/quote?symbol=XAU/USD&apikey={TWELVE_DATA_API_KEY}"
    try:
        r = requests.get(url, timeout=6)
        data = r.json() if r.content else {}
        if r.status_code != 200 or data.get("status") == "error":
            message = data.get("message") or f"HTTP {r.status_code}"
            _record_twelve_data_failure(message)
            logger.warning("[TWELVE_DATA] Quote failed: %s", message)
            return None
        price = _finite_positive(data.get("close") or data.get("price"))
        if not price or price <= 1000:
            _record_twelve_data_failure("Invalid XAU/USD live price")
            return None
        bid = _finite_positive(data.get("bid"))
        ask = _finite_positive(data.get("ask"))
        spread_available = bid is not None and ask is not None and ask >= bid
        mid = (bid + ask) / 2.0 if spread_available else price
        source_dt = _parse_price_feed_timestamp(data.get("datetime") or data.get("timestamp")) or datetime.now(timezone.utc)
        now_dt = datetime.now(timezone.utc)
        age = max(0.0, (now_dt - source_dt).total_seconds())
        with TWELVE_DATA_LOCK:
            LAST_TWELVE_DATA_QUOTE_TIME = time.monotonic()
        feed = {
            "symbol": "XAU/USD",
            "provider": "Twelve Data",
            "status": "ACTIVE" if age <= PRICE_FEED_STALE_SECONDS else "STALE",
            "bid": round(bid, 6) if bid is not None else None,
            "ask": round(ask, 6) if ask is not None else None,
            "mid": round(mid, 6),
            "spot": round(price, 6),
            "timestamp": source_dt.isoformat(),
            "source_timestamp": source_dt.isoformat(),
            "fetched_timestamp": now_dt.isoformat(),
            "age_seconds": round(age, 3),
            "error_type": None,
            "error_message": None,
            "spread_available": bool(spread_available),
            "execution_ready": bool(spread_available and age <= PRICE_FEED_EXECUTION_MAX_AGE),
        }
        _record_twelve_data_success()
        return feed
    except Exception as e:
        _record_twelve_data_failure(str(e))
        logger.warning("[TWELVE_DATA] Exception fetching live quote: %s", e)
        return None

'''
text, n = quote_pattern.subn(quote_replacement, text, count=1)
if n != 1:
    raise SystemExit("live quote function not found")

ohlc_pattern = re.compile(r"def fetch_twelve_data_ohlc\(.*?(?=def fetch_canonical_xauusd_feed)", re.S)
ohlc_replacement = '''def fetch_twelve_data_ohlc(symbol="XAU/USD", interval="15min", outputsize=150, request_class="background"):
    """Fetch historical OHLC through the centralized Twelve Data budget gateway."""
    if not TWELVE_DATA_API_KEY:
        return pd.DataFrame()
    td_symbol = "XAU/USD" if "XAU" in symbol.upper() else symbol
    td_interval = "15min" if interval in ("15m", "15min") else ("1h" if interval in ("1h", "60min") else "1day")
    key = (td_symbol.upper(), td_interval)
    now = time.monotonic()
    with TWELVE_DATA_LOCK:
        last_request = float(TWELVE_DATA_OHLC_LAST_REQUESTS.get(key, 0.0) or 0.0)
        if last_request and now - last_request < TWELVE_DATA_OHLC_INTERVAL:
            return pd.DataFrame()
    if not _twelve_data_request_allowed("ohlc", request_class=request_class):
        return pd.DataFrame()
    _record_twelve_data_credit(request_class=request_class)
    url = f"https://api.twelvedata.com/time_series?symbol={td_symbol}&interval={td_interval}&outputsize={outputsize}&apikey={TWELVE_DATA_API_KEY}"
    try:
        r = requests.get(url, timeout=8)
        data = r.json() if r.content else {}
        with TWELVE_DATA_LOCK:
            TWELVE_DATA_OHLC_LAST_REQUESTS[key] = time.monotonic()
        if r.status_code != 200 or data.get("status") == "error":
            message = data.get("message") or f"HTTP {r.status_code}"
            _record_twelve_data_failure(message)
            logger.warning("[TWELVE_DATA] OHLC failed: %s", message)
            return pd.DataFrame()
        values = data.get("values", [])
        if not values:
            _record_twelve_data_failure("Twelve Data returned no OHLC values")
            return pd.DataFrame()
        df = pd.DataFrame(values)
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
            df = df.set_index("datetime").sort_index()
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
        for col in ["Open", "High", "Low", "Close"]:
            if col not in df.columns:
                _record_twelve_data_failure(f"Missing OHLC field: {col}")
                return pd.DataFrame()
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if "Volume" in df.columns:
            df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0)
        else:
            df["Volume"] = 0.0
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if df.empty:
            _record_twelve_data_failure("Validated OHLC data is empty")
            return pd.DataFrame()
        _record_twelve_data_success()
        return df
    except Exception as e:
        _record_twelve_data_failure(str(e))
        logger.warning("[TWELVE_DATA] Exception fetching OHLC: %s", e)
        return pd.DataFrame()

'''
text, n = ohlc_pattern.subn(ohlc_replacement, text, count=1)
if n != 1:
    raise SystemExit("OHLC function not found")

canonical_pattern = re.compile(r"def fetch_canonical_xauusd_feed\(\):.*?(?=def get_xauusd_execution_price)", re.S)
canonical_replacement = '''def fetch_canonical_xauusd_feed(request_class="background"):
    """Canonical XAU/USD spot from Twelve Data only; no candle-as-live fallback."""
    now = time.monotonic()
    cached = CANONICAL_XAUUSD_FEED_CACHE.get("feed")
    last_attempt = float(CANONICAL_XAUUSD_FEED_CACHE.get("last_request_monotonic", 0.0) or 0.0)
    if cached:
        refreshed = _refresh_cached_feed_status(cached)
        if refreshed.get("status") == "ACTIVE":
            CANONICAL_XAUUSD_FEED_CACHE["feed"] = refreshed
            if now - last_attempt < max(3.0, TWELVE_DATA_LIVE_INTERVAL):
                return refreshed
    CANONICAL_XAUUSD_FEED_CACHE["last_request_monotonic"] = now
    feed = fetch_twelve_data_live_quote(request_class=request_class) if TWELVE_DATA_API_KEY else None
    if feed and feed.get("status") == "ACTIVE":
        CANONICAL_XAUUSD_FEED_CACHE["feed"] = feed
        return feed
    if cached:
        return _refresh_cached_feed_status(cached)
    return _build_missing_twelve_feed("unavailable", "تعذر الحصول على سعر XAU/USD المباشر من Twelve Data حالياً.")

'''
text, n = canonical_pattern.subn(canonical_replacement, text, count=1)
if n != 1:
    raise SystemExit("canonical feed function not found")

exec_pattern = re.compile(r"def get_xauusd_execution_price\(.*?(?=def fetch_live_spot_gold)", re.S)
exec_replacement = '''def get_xauusd_execution_price(feed, direction, role):
    direction = str(direction).upper()
    role = str(role).upper()
    if not feed or feed.get("status") != "ACTIVE":
        return None
    if feed.get("age_seconds") is None or float(feed.get("age_seconds")) > PRICE_FEED_EXECUTION_MAX_AGE:
        return None
    if role == "ENTRY":
        return feed.get("ask") if direction == "BUY" else feed.get("bid")
    if role == "EXIT":
        return feed.get("bid") if direction == "BUY" else feed.get("ask")
    return feed.get("mid") or feed.get("spot")

'''
text, n = exec_pattern.subn(exec_replacement, text, count=1)
if n != 1:
    raise SystemExit("execution pricing function not found")

text = re.sub(r"def fetch_live_spot_gold\(\):", "def fetch_live_spot_gold(request_class=\"background\"):", text, count=1)
text = re.sub(r"feed = fetch_canonical_xauusd_feed\(\)\n    return float\(feed\.get\('mid'\)\) if feed\.get\('status'\) == 'ACTIVE' and feed\.get\('mid'\) else 0\.0", "feed = fetch_canonical_xauusd_feed(request_class=request_class)\n    return float(feed.get('mid')) if feed.get('status') == 'ACTIVE' and feed.get('mid') else 0.0", text, count=1)

text = re.sub(r"def get_market_data\(\):", "def get_market_data(request_class=\"background\"):", text, count=1)
text = re.sub(r"def get_market_data\(request_class=\"background\"\):\n    feed = fetch_canonical_xauusd_feed\(\)", "def get_market_data(request_class=\"background\"):\n    feed = fetch_canonical_xauusd_feed(request_class=request_class)", text, count=1)

text = re.sub(r"def fetch_and_update_cache\(\):\n    try:\n        feed = fetch_canonical_xauusd_feed\(\)", "def fetch_and_update_cache(request_class=\"background\"):\n    try:\n        feed = fetch_canonical_xauusd_feed(request_class=request_class)", text, count=1)

chart_pattern = re.compile(r"def get_chart_data_cached\(\):.*?(?=def get_verified_closed_m15)", re.S)
chart_replacement = '''def get_chart_data_cached(request_class="background"):
    now = datetime.now(timezone.utc)
    with cache_lock:
        last_fetch_by_interval = MARKET_DATA_CACHE.setdefault("last_fetch_by_interval", {})
        h1_fresh = (now - last_fetch_by_interval.get("1h", datetime.min.replace(tzinfo=timezone.utc))).total_seconds() < TWELVE_DATA_OHLC_INTERVAL
        m15_fresh = (now - last_fetch_by_interval.get("15min", datetime.min.replace(tzinfo=timezone.utc))).total_seconds() < TWELVE_DATA_OHLC_INTERVAL
        has_h1 = not MARKET_DATA_CACHE["df_gold_h1"].empty
        has_m15 = not MARKET_DATA_CACHE["df_gold_m15"].empty
        snapshot = MARKET_DATA_CACHE.copy()
    if has_h1 and has_m15 and h1_fresh and m15_fresh:
        return snapshot
    with fetch_lock:
        now = datetime.now(timezone.utc)
        with cache_lock:
            last_fetch_by_interval = MARKET_DATA_CACHE.setdefault("last_fetch_by_interval", {})
            h1_fresh = (now - last_fetch_by_interval.get("1h", datetime.min.replace(tzinfo=timezone.utc))).total_seconds() < TWELVE_DATA_OHLC_INTERVAL
            m15_fresh = (now - last_fetch_by_interval.get("15min", datetime.min.replace(tzinfo=timezone.utc))).total_seconds() < TWELVE_DATA_OHLC_INTERVAL
        if not h1_fresh:
            df_gold_h1 = fetch_twelve_data_ohlc("XAU/USD", interval="1h", outputsize=150, request_class=request_class)
            if not df_gold_h1.empty:
                with cache_lock:
                    MARKET_DATA_CACHE["df_gold_h1"] = df_gold_h1
                    MARKET_DATA_CACHE["last_fetch_by_interval"]["1h"] = now
        if not m15_fresh:
            df_gold_m15 = fetch_twelve_data_ohlc("XAU/USD", interval="15min", outputsize=150, request_class=request_class)
            if not df_gold_m15.empty:
                with cache_lock:
                    MARKET_DATA_CACHE["df_gold_m15"] = df_gold_m15
                    MARKET_DATA_CACHE["last_fetch_by_interval"]["15min"] = now
    with cache_lock:
        return MARKET_DATA_CACHE.copy()

'''
text, n = chart_pattern.subn(chart_replacement, text, count=1)
if n != 1:
    raise SystemExit("chart cache function not found")

text = re.sub(r"def analyze_institutional_engine\(\):", "def analyze_institutional_engine(request_class=\"background\"):", text, count=1)
text = re.sub(r"cache = get_chart_data_cached\(\)", "cache = get_chart_data_cached(request_class=request_class)", text, count=1)
text = re.sub(r"spot_data = get_market_data\(\)", "spot_data = get_market_data(request_class=request_class)", text, count=1)

text = re.sub(r"def generate_quant_signal\(\):", "def generate_quant_signal(request_class=\"background\"):", text, count=1)
text = re.sub(r"data_quick = get_market_data\(\)", "data_quick = get_market_data(request_class=request_class)", text)
text = re.sub(r"data = analyze_institutional_engine\(\)", "data = analyze_institutional_engine(request_class=request_class)", text, count=1)
text = re.sub(r"get_market_data\(\)\.get\('gold'", "get_market_data(request_class=request_class).get('gold'", text, count=1)

text = re.sub(r"def run_quant_backtest\(\):", "def run_quant_backtest(request_class=\"background\"):", text, count=1)
# Backtest has its own first cache() occurrence later than the earlier engine occurrence.
backtest_start = text.find("def run_quant_backtest(request_class=\"background\"):")
if backtest_start >= 0:
    prefix, suffix = text[:backtest_start], text[backtest_start:]
    suffix = suffix.replace("cache = get_chart_data_cached()", "cache = get_chart_data_cached(request_class=request_class)", 1)
    text = prefix + suffix

text = re.sub(r"def generate_ai_market_insights\(\):", "def generate_ai_market_insights(request_class=\"background\"):", text, count=1)
text = re.sub(r"engine_res = analyze_institutional_engine\(\)", "engine_res = analyze_institutional_engine(request_class=request_class)", text, count=1)

# Manual handlers must explicitly use the reserved pool.
text = re.sub(r"sig = await asyncio\.to_thread\(generate_quant_signal\)", "sig = await asyncio.to_thread(generate_quant_signal, \"manual\")", text, count=1)
text = re.sub(r"ai_report = await asyncio\.to_thread\(generate_ai_market_insights\)", "ai_report = await asyncio.to_thread(generate_ai_market_insights, \"manual\")", text, count=1)
text = re.sub(r"res = await asyncio\.to_thread\(analyze_institutional_engine\)", "res = await asyncio.to_thread(analyze_institutional_engine, \"manual\")", text, count=1)
text = re.sub(r"report = await asyncio\.to_thread\(run_quant_backtest\)", "report = await asyncio.to_thread(run_quant_backtest, \"manual\")", text, count=1)
text = re.sub(r"data = get_market_data\(\)\n    feed = data\.get\('price_feed'\)", "data = get_market_data(request_class=\"manual\")\n    feed = data.get('price_feed')", text, count=1)
text = re.sub(r"gold_price = await asyncio\.to_thread\(fetch_live_spot_gold\)", "gold_price = await asyncio.to_thread(fetch_live_spot_gold, \"manual\")", text, count=1)
text = re.sub(r"market_cache = await asyncio\.to_thread\(get_chart_data_cached\)", "market_cache = await asyncio.to_thread(get_chart_data_cached, \"manual\")", text, count=1)
text = re.sub(r"engine_res = await asyncio\.to_thread\(analyze_institutional_engine\)", "engine_res = await asyncio.to_thread(analyze_institutional_engine, \"manual\")", text, count=1)

# Ensure reset clears the per-interval cache metadata.
text = text.replace('"last_fetch": None\n}', '"last_fetch": None,\n    "last_fetch_by_interval": {}\n}', 1)
text = text.replace('MARKET_DATA_CACHE["last_fetch"] = None', 'MARKET_DATA_CACHE["last_fetch"] = None\n        MARKET_DATA_CACHE["last_fetch_by_interval"] = {}', 1)

BOT.write_text(text, encoding="utf-8")
print("Applied Twelve Data 750-background/50-manual budget patch.")

from pathlib import Path

BOT = Path('bot.py')
text = BOT.read_text(encoding='utf-8')

old_state_start = "FEATURE_VERSION = os.getenv('FEATURE_VERSION', 'v2.8-features-1')"
old_state_end = "RAM_WARNING_MB = float(os.getenv('RAM_WARNING_MB', '400'))"
a = text.index(old_state_start)
b = text.index(old_state_end, a)
state = '''FEATURE_VERSION = os.getenv('FEATURE_VERSION', 'v2.8-features-1')
STRATEGY_VERSION = os.getenv('STRATEGY_VERSION', 'v2.8-flexible')
MODEL_VERSION = os.getenv('MODEL_VERSION', 'rf-v1')
PRICE_FEED_STALE_SECONDS = int(os.getenv('PRICE_FEED_STALE_SECONDS', '240'))
PRICE_FEED_MIN_REQUEST_INTERVAL = 150.0
PRICE_FEED_LOCK = threading.Lock()
CANONICAL_XAUUSD_FEED_CACHE = {'feed': None, 'last_request_monotonic': 0.0, 'blocked_until': 0.0}

TWELVE_DATA_API_KEY = os.getenv('TWELVE_DATA_API_KEY', '').strip()
TWELVE_DATA_BASE_URL = 'https://api.twelvedata.com'
TWELVE_DATA_SYMBOL = 'XAU/USD'
TWELVE_DATA_DAILY_LIMIT = 800
TWELVE_DATA_CALLS_PER_MINUTE = 8
TWELVE_DATA_LIVE_INTERVAL_SECONDS = 150
TWELVE_DATA_M15_REFRESH_SECONDS = 1800
TWELVE_DATA_H1_REFRESH_SECONDS = 7200
TWELVE_DATA_MIN_GAP_SECONDS = 8.0
TWELVE_DATA_RETRY_COOLDOWN_SECONDS = 300
TWELVE_DATA_REQUEST_TIMES = []
TWELVE_DATA_DAILY_STATE = {'date': None, 'calls': 0, 'blocked_until': 0.0, 'last_error': None}
TWELVE_DATA_RATE_LOCK = threading.Lock()
TWELVE_DATA_HIST_CACHE = {'m15': None, 'h1': None, 'm15_at': 0.0, 'h1_at': 0.0}
TWELVE_DATA_HIST_CACHE_LOCK = threading.Lock()

'''
text = text[:a] + state + text[b:]

block_start = "# ------------------------------------\n# 4. محرك بيانات XAU/USD المعزول: Yahoo Finance Spot\n# ------------------------------------"
block_end = "def get_verified_closed_m15(df):"
a = text.index(block_start)
b = text.index(block_end, a)
provider = '''# ------------------------------------
# 4. محرك بيانات XAU/USD المعزول: Twelve Data
# ------------------------------------

def _parse_price_feed_timestamp(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.isdigit():
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None

def _finite_positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None

def _build_missing_twelvedata_feed(error_type='missing', error_message='No live XAU/USD data available.'):
    now = datetime.now(timezone.utc)
    return {
        'symbol': TWELVE_DATA_SYMBOL,
        'provider': 'Twelve Data',
        'status': 'MISSING',
        'bid': None,
        'ask': None,
        'mid': None,
        'spot': None,
        'source_timestamp': None,
        'timestamp': None,
        'fetched_timestamp': now.isoformat(),
        'source_fetched_timestamp': None,
        'age_seconds': None,
        'error_type': str(error_type),
        'error_message': str(error_message or '')[:300],
        'spread_available': False,
    }

def _refresh_cached_twelvedata_feed(feed):
    if not feed:
        return _build_missing_twelvedata_feed('no_cached_feed', 'Twelve Data XAU/USD cache is empty.')
    refreshed = dict(feed)
    source_ts = _parse_price_feed_timestamp(refreshed.get('source_timestamp'))
    if source_ts is None or refreshed.get('mid') is None:
        refreshed['status'] = 'MISSING'
        return refreshed
    age = max(0.0, (datetime.now(timezone.utc) - source_ts).total_seconds())
    refreshed['age_seconds'] = round(age, 3)
    refreshed['status'] = 'STALE' if age > PRICE_FEED_STALE_SECONDS else 'ACTIVE'
    return refreshed

def _twelvedata_rate_gate():
    while True:
        now = time.monotonic()
        with TWELVE_DATA_RATE_LOCK:
            times = [t for t in TWELVE_DATA_REQUEST_TIMES if now - t < 60.0]
            TWELVE_DATA_REQUEST_TIMES[:] = times
            last = times[-1] if times else None
            wait_gap = (last + TWELVE_DATA_MIN_GAP_SECONDS - now) if last is not None else 0.0
            if len(times) < TWELVE_DATA_CALLS_PER_MINUTE and wait_gap <= 0:
                TWELVE_DATA_REQUEST_TIMES.append(now)
                return
            oldest = times[0] if times else now
        time.sleep(max(0.25, min(oldest + 60.0 - now, 10.0)))

def _twelvedata_budget_guard():
    today = datetime.now(timezone.utc).date().isoformat()
    now = time.monotonic()
    with TWELVE_DATA_RATE_LOCK:
        if TWELVE_DATA_DAILY_STATE.get('date') != today:
            TWELVE_DATA_DAILY_STATE.update(date=today, calls=0, blocked_until=0.0, last_error=None)
        if now < float(TWELVE_DATA_DAILY_STATE.get('blocked_until', 0.0) or 0.0):
            return False, 'cooldown'
        if int(TWELVE_DATA_DAILY_STATE.get('calls', 0) or 0) >= TWELVE_DATA_DAILY_LIMIT:
            return False, 'daily_limit'
        TWELVE_DATA_DAILY_STATE['calls'] += 1
        return True, None

def _twelvedata_request(endpoint, params, timeout=8):
    if not TWELVE_DATA_API_KEY:
        return None, {'error_type': 'missing_api_key', 'status': None, 'message': 'TWELVE_DATA_API_KEY is not configured.'}
    allowed, reason = _twelvedata_budget_guard()
    if not allowed:
        return None, {'error_type': reason, 'status': 429, 'message': 'Twelve Data daily budget/cooldown gate is active.'}
    _twelvedata_rate_gate()
    headers = {'Authorization': f'apikey {TWELVE_DATA_API_KEY}', 'Accept': 'application/json', 'User-Agent': 'gold-quant-bot/1.0'}
    try:
        response = requests.get(f'{TWELVE_DATA_BASE_URL}/{endpoint.lstrip("/")}', params=params, headers=headers, timeout=timeout)
    except requests.Timeout as exc:
        with TWELVE_DATA_RATE_LOCK:
            TWELVE_DATA_DAILY_STATE['blocked_until'] = time.monotonic() + TWELVE_DATA_RETRY_COOLDOWN_SECONDS
            TWELVE_DATA_DAILY_STATE['last_error'] = str(exc)[:250]
        return None, {'error_type': 'timeout', 'status': None, 'message': str(exc)[:250]}
    except requests.RequestException as exc:
        with TWELVE_DATA_RATE_LOCK:
            TWELVE_DATA_DAILY_STATE['blocked_until'] = time.monotonic() + TWELVE_DATA_RETRY_COOLDOWN_SECONDS
            TWELVE_DATA_DAILY_STATE['last_error'] = str(exc)[:250]
        return None, {'error_type': 'network_error', 'status': None, 'message': str(exc)[:250]}

    status = int(response.status_code)
    try:
        payload = response.json()
    except Exception:
        payload = None
    if status == 200 and isinstance(payload, dict) and payload.get('status') != 'error':
        return payload, {'error_type': None, 'status': status, 'message': None}

    message = str((payload or {}).get('message') or (payload or {}).get('code') or getattr(response, 'text', '') or '')[:250]
    cooldown = TWELVE_DATA_RETRY_COOLDOWN_SECONDS
    with TWELVE_DATA_RATE_LOCK:
        TWELVE_DATA_DAILY_STATE['blocked_until'] = time.monotonic() + cooldown
        TWELVE_DATA_DAILY_STATE['last_error'] = message
    return None, {'error_type': f'http_{status}', 'status': status, 'message': message}

def fetch_twelvedata_time_series(interval, outputsize):
    payload, error = _twelvedata_request('time_series', {
        'symbol': TWELVE_DATA_SYMBOL,
        'interval': str(interval),
        'outputsize': int(outputsize),
        'timezone': 'UTC',
        'apikey': TWELVE_DATA_API_KEY,
    }, timeout=10)
    if not payload:
        logger.warning('[MARKET_DATA] Twelve Data time_series interval=%s status=%s type=%s message=%s', interval, error.get('status'), error.get('error_type'), error.get('message'))
        return pd.DataFrame()
    rows = []
    for item in payload.get('values') or []:
        try:
            rows.append({
                'datetime': pd.to_datetime(item['datetime'], utc=True),
                'Open': float(item['open']),
                'High': float(item['high']),
                'Low': float(item['low']),
                'Close': float(item['close']),
                'Volume': float(item.get('volume') or 0.0),
            })
        except (TypeError, ValueError, KeyError):
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index('datetime').sort_index()
    return df[~df.index.duplicated(keep='last')]

def fetch_canonical_xauusd_feed():
    now = time.monotonic()
    with PRICE_FEED_LOCK:
        cached = CANONICAL_XAUUSD_FEED_CACHE.get('feed')
        last_attempt = float(CANONICAL_XAUUSD_FEED_CACHE.get('last_request_monotonic', 0.0) or 0.0)
        blocked_until = float(CANONICAL_XAUUSD_FEED_CACHE.get('blocked_until', 0.0) or 0.0)
        if now < blocked_until:
            return _refresh_cached_twelvedata_feed(cached) if cached else _build_missing_twelvedata_feed('cooldown', 'Twelve Data live request cooldown is active.')
        if now - last_attempt < TWELVE_DATA_LIVE_INTERVAL_SECONDS:
            return _refresh_cached_twelvedata_feed(cached) if cached else _build_missing_twelvedata_feed('rate_gate', 'Twelve Data live request gate is active.')
        CANONICAL_XAUUSD_FEED_CACHE['last_request_monotonic'] = now

    payload, error = _twelvedata_request('price', {'symbol': TWELVE_DATA_SYMBOL, 'apikey': TWELVE_DATA_API_KEY}, timeout=8)
    if not payload:
        cached = CANONICAL_XAUUSD_FEED_CACHE.get('feed')
        feed = _refresh_cached_twelvedata_feed(cached) if cached else _build_missing_twelvedata_feed(error.get('error_type') or 'request_failed', error.get('message'))
        if cached:
            feed = dict(feed)
            feed['status'] = 'STALE'
            feed['error_type'] = error.get('error_type')
            feed['error_message'] = error.get('message')
        CANONICAL_XAUUSD_FEED_CACHE['blocked_until'] = time.monotonic() + TWELVE_DATA_RETRY_COOLDOWN_SECONDS
        CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
        logger.warning('[XAUUSD_FEED] Twelve Data cooldown type=%s http_status=%s message=%s', error.get('error_type'), error.get('status'), error.get('message'))
        return feed

    price = _finite_positive(payload.get('price'))
    if price is None:
        feed = _build_missing_twelvedata_feed('missing_price', 'Twelve Data price response did not contain a valid price.')
        CANONICAL_XAUUSD_FEED_CACHE['blocked_until'] = time.monotonic() + TWELVE_DATA_RETRY_COOLDOWN_SECONDS
        CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
        return feed

    source_dt = datetime.now(timezone.utc)
    feed = {
        'symbol': TWELVE_DATA_SYMBOL,
        'provider': 'Twelve Data',
        'status': 'ACTIVE',
        'bid': None,
        'ask': None,
        'mid': round(price, 6),
        'spot': round(price, 6),
        'timestamp': source_dt.isoformat(),
        'source_timestamp': source_dt.isoformat(),
        'fetched_timestamp': source_dt.isoformat(),
        'age_seconds': 0.0,
        'error_type': None,
        'error_message': None,
        'spread_available': False,
    }
    CANONICAL_XAUUSD_FEED_CACHE['blocked_until'] = 0.0
    CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
    return feed

def get_xauusd_execution_price(feed, direction, role):
    direction = str(direction).upper()
    role = str(role).upper()
    if not feed or feed.get('status') != 'ACTIVE':
        return None
    if role in {'ENTRY', 'EXIT'}:
        if direction == 'BUY' and feed.get('ask') is not None:
            return feed['ask']
        if direction == 'SELL' and feed.get('bid') is not None:
            return feed['bid']
        return feed.get('mid') or feed.get('spot')
    return feed.get('mid') or feed.get('spot')

def fetch_live_spot_gold():
    feed = fetch_canonical_xauusd_feed()
    return float(feed.get('mid')) if feed.get('status') == 'ACTIVE' and feed.get('mid') else 0.0

def get_market_data():
    feed = fetch_canonical_xauusd_feed()
    spot = feed.get('mid') or feed.get('spot') or 0.0
    with cache_lock:
        snapshot = dict(GLOBAL_CACHE.get('market_data') or {})
    return {'gold': round(float(spot), 2) if spot else 0.0, 'dxy': snapshot.get('dxy'), 'us10y': snapshot.get('us10y'), 'price_feed': feed}

def fetch_and_update_cache():
    try:
        feed = fetch_canonical_xauusd_feed()
        gold = feed.get('mid') or feed.get('spot') or 0.0
        with cache_lock:
            previous = GLOBAL_CACHE.get('market_data') or {}
            GLOBAL_CACHE['market_data'] = {
                'gold': round(float(gold), 2) if gold else 0.0,
                'dxy': previous.get('dxy'),
                'us10y': previous.get('us10y'),
                'price_feed': feed,
            }
            if gold:
                GLOBAL_CACHE['last_updated'] = datetime.now(timezone.utc)
    except Exception as exc:
        logger.warning('[MARKET_DATA] Twelve Data cache refresh failure type=%s message=%s', type(exc).__name__, str(exc)[:300])

def get_chart_data_cached():
    now = time.monotonic()
    with TWELVE_DATA_HIST_CACHE_LOCK:
        cached_m15 = TWELVE_DATA_HIST_CACHE.get('m15')
        cached_h1 = TWELVE_DATA_HIST_CACHE.get('h1')
        m15_at = float(TWELVE_DATA_HIST_CACHE.get('m15_at', 0.0) or 0.0)
        h1_at = float(TWELVE_DATA_HIST_CACHE.get('h1_at', 0.0) or 0.0)

    if cached_m15 is None or now - m15_at >= TWELVE_DATA_M15_REFRESH_SECONDS:
        fresh_m15 = fetch_twelvedata_time_series('15min', 1000)
        if not fresh_m15.empty:
            cached_m15 = fresh_m15
            with TWELVE_DATA_HIST_CACHE_LOCK:
                TWELVE_DATA_HIST_CACHE['m15'] = fresh_m15
                TWELVE_DATA_HIST_CACHE['m15_at'] = now

    if cached_h1 is None or now - h1_at >= TWELVE_DATA_H1_REFRESH_SECONDS:
        fresh_h1 = fetch_twelvedata_time_series('1h', 1000)
        if not fresh_h1.empty:
            cached_h1 = fresh_h1
            with TWELVE_DATA_HIST_CACHE_LOCK:
                TWELVE_DATA_HIST_CACHE['h1'] = fresh_h1
                TWELVE_DATA_HIST_CACHE['h1_at'] = now

    return {
        'df_gold_h1': cached_h1.copy() if isinstance(cached_h1, pd.DataFrame) else pd.DataFrame(),
        'df_gold_m15': cached_m15.copy() if isinstance(cached_m15, pd.DataFrame) else pd.DataFrame(),
        'df_dxy_m15': pd.DataFrame(),
        'df_us10y_m15': pd.DataFrame(),
        'last_fetch': datetime.now(timezone.utc),
    }

'''
text = text[:a] + provider + text[b:]

# Remove the now-unused curl_cffi block.
curl_start = "# 🌐 استدعاء curl_cffi لتجاوز حظر Cloudflare وبصمات السيرفرات السحابية\ntry:\n    from curl_cffi import requests as curl_requests\n    HAS_CURL_CFFI = True\nexcept ImportError:\n    curl_requests = requests\n    HAS_CURL_CFFI = False\n\n"
text = text.replace(curl_start, '', 1)

text = text.replace("        await asyncio.sleep(5)\n\nasync def auto_market_scanner", "        await asyncio.sleep(30)\n\nasync def auto_market_scanner", 1)
text = text.replace('"dxy": 99.85, "us10y": 4.63, "price_feed": {"symbol": "XAUUSD", "provider": "Yahoo Finance"', '"dxy": None, "us10y": None, "price_feed": {"symbol": "XAU/USD", "provider": "Twelve Data"', 1)
text = text.replace("f\"Provider: {feed.get('provider', 'Yahoo Finance')}\\n\"", "f\"Provider: {feed.get('provider', 'Twelve Data')}\\n\"")

# Sanity checks: no hidden Yahoo/Argent market path must survive.
legacy = ('Yahoo', 'YAHOO_', 'yfinance', 'XAUUSD=X', 'ARGENT', 'ARGENT_API_KEY', 'api.argentapi.com', 'curl_cffi')
for token in legacy:
    if token.lower() in text.lower():
        raise SystemExit(f'Legacy provider reference remains: {token}')

BOT.write_text(text, encoding='utf-8')

req = Path('requirements.txt')
req_lines = [line for line in req.read_text(encoding='utf-8').splitlines() if line.strip() != 'curl_cffi>=0.7.0']
if not any(line.lower().startswith('requests') for line in req_lines):
    req_lines.append('requests>=2.32')
req.write_text('\n'.join(req_lines) + '\n', encoding='utf-8')

readme = Path('README.md')
r = readme.read_text(encoding='utf-8')
start = r.index('## بيانات السوق')
end = r.index('## التشغيل', start)
market = '''## بيانات السوق

Twelve Data هو المصدر الأساسي والوحيد لبيانات XAU/USD في الإنتاج. الرمز المستخدم هو `XAU/USD`.

- Live Spot: Twelve Data `/price`.
- Historical: Twelve Data `/time_series` مع `15min` و`1h`.
- API key: متغير البيئة `TWELVE_DATA_API_KEY` على Render، ولا يتم تخزين المفتاح داخل Git.
- خطة التشغيل المجانية: 800 API credits/day و8 credits/minute.
- الاستهلاك المخطط للبوت: حوالي 636 طلبًا/يوم في التشغيل الطبيعي (576 Live + 48 M15 + 12 H1)، مع كاش وعدم وجود retry loop.
- عند 429 أو فشل الشبكة يدخل المزود في cooldown ولا يتم اصطناع سعر بديل.
- Bid/Ask يبقيان `None` عندما لا يقدمهما Twelve Data؛ لا يتم اختراع spread.
- DXY وUS10Y لا يتم جلبهما من مزود خارجي حتى لا تعود تبعية Yahoo إلى مسار السوق.

'''
readme.write_text(r[:start] + market + r[end:], encoding='utf-8')

print('Twelve Data migration patch applied')

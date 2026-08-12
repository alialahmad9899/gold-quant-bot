from pathlib import Path
import ast
import re

BOT = Path('bot.py')
REQ = Path('requirements.txt')
s = BOT.read_text(encoding='utf-8')

s = re.sub(r'^import yfinance as yf\n', '', s, flags=re.M)
for name, default in {
    'YAHOO_LIVE_COOLDOWN_SECONDS': '60',
    'YAHOO_HISTORICAL_COOLDOWN_SECONDS': '300',
    'YAHOO_HISTORICAL_FETCH_INTERVAL_SECONDS': '300',
}.items():
    s = re.sub(rf'^{re.escape(name)}\s*=.*$', f"{name} = int(os.getenv('{name}', '{default}'))", s, count=1, flags=re.M)

state_marker = "YAHOO_HISTORICAL_STATE_LOCK = threading.Lock()"
if 'YAHOO_LIVE_REQUEST_LOCK = threading.Lock()' not in s:
    if state_marker not in s:
        raise SystemExit('Yahoo state marker not found')
    s = s.replace(state_marker, state_marker + "\nYAHOO_LIVE_REQUEST_LOCK = threading.Lock()\nYAHOO_HISTORICAL_REQUEST_LOCK = threading.Lock()\nYAHOO_HIST_CACHE = {}\nYAHOO_HTTP_DIAGNOSTIC = {'status': None, 'host': None, 'transport': None, 'error_type': None, 'message': None}", 1)

def replace_func(src, name, replacement):
    m = re.search(rf'^def {re.escape(name)}\(.*?(?=^def |\Z)', src, flags=re.M|re.S)
    if not m:
        raise SystemExit(f'missing function: {name}')
    return src[:m.start()] + replacement.rstrip() + "\n\n" + src[m.end():]

HTTP = r'''def http_get_yahoo(url, timeout=8):
    """One logical Yahoo request path with browser impersonation, precise diagnostics, and no fan-out."""
    global YAHOO_HTTP_DIAGNOSTIC
    for host in ('query1.finance.yahoo.com', 'query2.finance.yahoo.com'):
        target = url.replace('query1.finance.yahoo.com', host).replace('query2.finance.yahoo.com', host)
        if HAS_CURL_CFFI:
            try:
                r = curl_requests.get(target, impersonate='chrome124', timeout=timeout, verify=True)
                status = int(getattr(r, 'status_code', 0) or 0)
                YAHOO_HTTP_DIAGNOSTIC = {'status': status, 'host': host, 'transport': 'curl_cffi', 'error_type': None, 'message': None}
                if status == 200:
                    try:
                        data = r.json()
                    except Exception as exc:
                        YAHOO_HTTP_DIAGNOSTIC.update(error_type='invalid_json', message=str(exc)[:250])
                        return None
                    if isinstance(data, dict) and (data.get('chart') or {}).get('result'):
                        return data
                    YAHOO_HTTP_DIAGNOSTIC.update(error_type='missing_result', message='Yahoo response has no chart.result.')
                    return None
                YAHOO_HTTP_DIAGNOSTIC.update(error_type=f'http_{status}', message=str(getattr(r, 'text', '') or '')[:250])
                if status in (403, 404, 429):
                    return None
                if status >= 500:
                    continue
            except Exception as exc:
                YAHOO_HTTP_DIAGNOSTIC = {'status': None, 'host': host, 'transport': 'curl_cffi', 'error_type': type(exc).__name__, 'message': str(exc)[:250]}
                continue
        else:
            try:
                r = requests.get(target, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}, timeout=timeout)
                status = int(getattr(r, 'status_code', 0) or 0)
                YAHOO_HTTP_DIAGNOSTIC = {'status': status, 'host': host, 'transport': 'requests', 'error_type': None, 'message': None}
                if status == 200:
                    try:
                        data = r.json()
                    except Exception as exc:
                        YAHOO_HTTP_DIAGNOSTIC.update(error_type='invalid_json', message=str(exc)[:250])
                        return None
                    if isinstance(data, dict) and (data.get('chart') or {}).get('result'):
                        return data
                    YAHOO_HTTP_DIAGNOSTIC.update(error_type='missing_result', message='Yahoo response has no chart.result.')
                    return None
                YAHOO_HTTP_DIAGNOSTIC.update(error_type=f'http_{status}', message=str(getattr(r, 'text', '') or '')[:250])
                if status in (403, 404, 429) or status >= 500:
                    return None
            except Exception as exc:
                YAHOO_HTTP_DIAGNOSTIC = {'status': None, 'host': host, 'transport': 'requests', 'error_type': type(exc).__name__, 'message': str(exc)[:250]}
                continue
    return None
'''

HIST = r'''def fetch_yahoo_direct(symbol, range_str='10d', interval_str='15m'):
    """Fetch Yahoo OHLC through a per-symbol cache and single-flight request lock."""
    key = (str(symbol), str(interval_str), str(range_str))
    now = time.monotonic()
    cached = YAHOO_HIST_CACHE.get(key)
    ttl = YAHOO_HISTORICAL_FETCH_INTERVAL_SECONDS if symbol == 'XAUUSD=X' else 300
    if cached:
        if now < cached.get('blocked_until', 0.0):
            return cached.get('df', pd.DataFrame()).copy()
        if now - cached.get('last_success', 0.0) < ttl:
            return cached.get('df', pd.DataFrame()).copy()
    with YAHOO_HISTORICAL_REQUEST_LOCK:
        now = time.monotonic()
        cached = YAHOO_HIST_CACHE.get(key)
        if cached:
            if now < cached.get('blocked_until', 0.0):
                return cached.get('df', pd.DataFrame()).copy()
            if now - cached.get('last_success', 0.0) < ttl:
                return cached.get('df', pd.DataFrame()).copy()
        data = http_get_yahoo(f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval_str}&range={range_str}', timeout=8)
        if not data:
            diag = dict(YAHOO_HTTP_DIAGNOSTIC)
            status = diag.get('status')
            cooldown = YAHOO_HISTORICAL_COOLDOWN_SECONDS
            if status in (403, 429):
                cooldown = 900
            elif status is not None and status >= 500:
                cooldown = 120
            msg = diag.get('message') or 'Yahoo historical request failed.'
            logger.warning('[MARKET_DATA] Yahoo historical cooldown type=%s http_status=%s seconds=%s message=%s', diag.get('error_type') or 'network_error', status, cooldown, msg)
            old_df = cached.get('df', pd.DataFrame()).copy() if cached else pd.DataFrame()
            YAHOO_HIST_CACHE[key] = {'df': old_df, 'last_success': cached.get('last_success', 0.0) if cached else 0.0, 'blocked_until': now + cooldown}
            return old_df.copy()
        try:
            result = ((data.get('chart') or {}).get('result') or [None])[0]
            if not result:
                raise ValueError('Yahoo response has no chart.result')
            timestamps = result.get('timestamp') or []
            quote = ((result.get('indicators') or {}).get('quote') or [None])[0]
            if not timestamps or not quote:
                raise ValueError('Yahoo response has no OHLC payload')
            df = pd.DataFrame({'Open': quote.get('open', []), 'High': quote.get('high', []), 'Low': quote.get('low', []), 'Close': quote.get('close', []), 'Volume': quote.get('volume', [0] * len(timestamps))}, index=pd.to_datetime(timestamps, unit='s', utc=True)).dropna(subset=['Open', 'High', 'Low', 'Close'])
            if df.empty:
                raise ValueError('Yahoo OHLC frame is empty')
            YAHOO_HIST_CACHE[key] = {'df': df.copy(), 'last_success': now, 'blocked_until': 0.0}
            return df
        except Exception as exc:
            logger.warning('[MARKET_DATA] Yahoo historical parse failure type=%s message=%s', type(exc).__name__, str(exc)[:300])
            YAHOO_HIST_CACHE[key] = {'df': pd.DataFrame(), 'last_success': 0.0, 'blocked_until': now + YAHOO_HISTORICAL_COOLDOWN_SECONDS}
            return pd.DataFrame()
'''

LIVE = r'''def fetch_canonical_xauusd_feed():
    """Canonical XAU/USD Spot from Yahoo XAUUSD=X with a hard 60s request gate."""
    now = time.monotonic()
    with YAHOO_LIVE_REQUEST_LOCK:
        cached = CANONICAL_XAUUSD_FEED_CACHE.get('feed')
        last_attempt = float(CANONICAL_XAUUSD_FEED_CACHE.get('last_request_monotonic', 0.0) or 0.0)
        blocked_until = float(CANONICAL_XAUUSD_FEED_CACHE.get('blocked_until', 0.0) or 0.0)
        if now < blocked_until:
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('cooldown', 'Yahoo live request cooldown is active.')
        if now - last_attempt < 60.0:
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('rate_gate', 'Yahoo live request gate is active.')
        CANONICAL_XAUUSD_FEED_CACHE['last_request_monotonic'] = now
        payload = http_get_yahoo('https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?interval=1m&range=1d', timeout=8)
        if not payload:
            diag = dict(YAHOO_HTTP_DIAGNOSTIC)
            status = diag.get('status')
            cooldown = 900 if status in (403, 429) else (120 if status is not None and status >= 500 else 60)
            msg = diag.get('message') or 'Yahoo XAUUSD=X live quote request failed.'
            kind = diag.get('error_type') or 'network_error'
            CANONICAL_XAUUSD_FEED_CACHE['blocked_until'] = time.monotonic() + cooldown
            feed = _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed(kind, msg)
            if cached:
                feed = dict(feed); feed['status'] = 'STALE'; feed['error_type'] = kind; feed['error_message'] = msg
            CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
            logger.warning('[XAUUSD_FEED] Yahoo Spot cooldown type=%s http_status=%s seconds=%s message=%s', kind, status, cooldown, msg)
            return feed
        result = ((payload.get('chart') or {}).get('result') or [None])[0]
        meta = (result or {}).get('meta') or {}
        try:
            price = float(meta.get('regularMarketPrice'))
        except (TypeError, ValueError):
            price = None
        if price is None or price <= 1000:
            msg = 'Yahoo XAUUSD=X live payload did not contain a valid regularMarketPrice.'
            CANONICAL_XAUUSD_FEED_CACHE['blocked_until'] = time.monotonic() + 60
            feed = _build_missing_yahoo_feed('missing_price', msg)
            CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
            logger.warning('[XAUUSD_FEED] Yahoo Spot failure type=missing_price message=%s', msg)
            return feed
        def _f(v):
            try: return float(v) if v is not None else None
            except (TypeError, ValueError): return None
        bid, ask = _f(meta.get('bid')), _f(meta.get('ask'))
        mid = (bid + ask) / 2.0 if bid and ask and bid > 0 and ask > 0 else price
        ts = meta.get('regularMarketTime')
        source_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else datetime.now(timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - source_dt).total_seconds())
        feed = {'symbol': 'XAUUSD=X', 'provider': 'Yahoo Finance', 'status': 'ACTIVE' if age <= PRICE_FEED_STALE_SECONDS else 'STALE', 'bid': bid, 'ask': ask, 'mid': round(float(mid), 6), 'spot': round(float(price), 6), 'timestamp': source_dt.isoformat(), 'fetched_timestamp': datetime.now(timezone.utc).isoformat(), 'age_seconds': round(age, 3), 'error_type': None, 'error_message': None, 'spread_available': bool(bid and ask)}
        CANONICAL_XAUUSD_FEED_CACHE['blocked_until'] = 0.0
        CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
        return feed
'''

for name, repl in [('http_get_yahoo', HTTP), ('fetch_yahoo_direct', HIST), ('fetch_canonical_xauusd_feed', LIVE)]:
    s = replace_func(s, name, repl)

s = replace_func(s, 'fetch_live_spot_gold', '''def fetch_live_spot_gold():
    """Return live/cached XAU/USD Spot only; never use historical Close as live fallback."""
    feed = fetch_canonical_xauusd_feed()
    return float(feed.get('mid')) if feed.get('status') == 'ACTIVE' and feed.get('mid') else 0.0
''')

s = re.sub(r"CANONICAL_XAUUSD_FEED_CACHE\s*=\s*\{[^\n]*\}", "CANONICAL_XAUUSD_FEED_CACHE = {'feed': None, 'last_request_monotonic': 0.0, 'blocked_until': 0.0}", s, count=1)

ast.parse(s)
BOT.write_text(s, encoding='utf-8')

if REQ.exists():
    lines = [x for x in REQ.read_text(encoding='utf-8').splitlines() if x.strip().lower() != 'yfinance']
    REQ.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8')
print('PATCH_OK')

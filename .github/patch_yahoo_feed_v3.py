from pathlib import Path
import ast
import math
import subprocess
import textwrap

BOT = Path('bot.py')
text = BOT.read_text(encoding='utf-8')
original = text

old_import = """try:\n    from curl_cffi import requests as curl_requests\nexcept ImportError:\n    curl_requests = requests\n"""
new_import = """try:\n    from curl_cffi import requests as curl_requests\n    HAS_CURL_CFFI = True\nexcept ImportError:\n    curl_requests = requests\n    HAS_CURL_CFFI = False\n"""
if old_import in text:
    text = text.replace(old_import, new_import, 1)
elif 'HAS_CURL_CFFI' not in text:
    raise RuntimeError('curl_cffi import anchor not found')


def replace_function(src, name, replacement):
    tree = ast.parse(src)
    node = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)
    if node is None:
        raise RuntimeError(f'function not found: {name}')
    lines = src.splitlines(True)
    return ''.join(lines[:node.lineno - 1] + [textwrap.dedent(replacement).strip('\n') + '\n\n'] + lines[node.end_lineno:])

text = replace_function(text, 'fetch_yahoo_direct', r'''
def fetch_yahoo_direct(symbol, range_str='10d', interval_str='15m'):
    if _yahoo_historical_cooldown_active():
        return pd.DataFrame()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36', 'Accept': 'application/json', 'Referer': 'https://finance.yahoo.com/'}
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
    try:
        kwargs = {'timeout': 8}
        if HAS_CURL_CFFI:
            kwargs['impersonate'] = 'chrome124'
        response = curl_requests.get(url, params={'interval': interval_str, 'range': range_str}, headers=headers, **kwargs)
        status = int(getattr(response, 'status_code', 0) or 0)
        if status == 429:
            _set_yahoo_historical_cooldown('rate_limited', 'Yahoo historical HTTP 429 rate limit.')
            return pd.DataFrame()
        if status != 200:
            if status >= 500:
                _set_yahoo_historical_cooldown('server_error', f'Yahoo historical HTTP {status}.')
            return pd.DataFrame()
        payload = response.json()
        result = (payload.get('chart') or {}).get('result') or []
        if not result or not isinstance(result[0], dict):
            _set_yahoo_historical_cooldown('missing_result', 'Yahoo historical response missing chart.result.')
            return pd.DataFrame()
        timestamps = result[0].get('timestamp') or []
        quotes = ((result[0].get('indicators') or {}).get('quote') or [])
        if not timestamps or not quotes:
            _set_yahoo_historical_cooldown('missing_ohlc', f'Yahoo {symbol} {interval_str} returned no OHLC rows.')
            return pd.DataFrame()
        q = quotes[0]
        df = pd.DataFrame({'Open': q.get('open', []), 'High': q.get('high', []), 'Low': q.get('low', []), 'Close': q.get('close', []), 'Volume': q.get('volume', [0] * len(timestamps))}, index=pd.to_datetime(timestamps, unit='s', utc=True)).dropna(subset=['Open', 'High', 'Low', 'Close'])
        if df.empty:
            _set_yahoo_historical_cooldown('empty_ohlc', f'Yahoo {symbol} {interval_str} returned empty OHLC.')
            return pd.DataFrame()
        with YAHOO_HISTORICAL_STATE_LOCK:
            YAHOO_HISTORICAL_FEED_STATE['blocked_until'] = 0.0
            YAHOO_HISTORICAL_FEED_STATE['error_type'] = None
            YAHOO_HISTORICAL_FEED_STATE['error_message'] = None
        return df
    except Exception as exc:
        msg = f'{type(exc).__name__}: {exc}'[:300]
        low = msg.lower()
        kind = 'rate_limited' if any(x in low for x in ('429', 'too many requests', 'rate limit', 'yfratelimiterror')) else ('timeout' if 'timeout' in low else 'network_or_response')
        _set_yahoo_historical_cooldown(kind, msg)
        return pd.DataFrame()
''')

text = replace_function(text, 'fetch_canonical_xauusd_feed', r'''
def fetch_canonical_xauusd_feed():
    """Canonical live XAUUSD Spot from Yahoo XAUUSD=X only; no provider fallback."""
    now_m = time.monotonic()
    with PRICE_FEED_LOCK:
        cached = CANONICAL_XAUUSD_FEED_CACHE.get('feed')
        last = float(CANONICAL_XAUUSD_FEED_CACHE.get('last_request_monotonic', 0.0) or 0.0)
        if now_m - last < PRICE_FEED_MIN_REQUEST_INTERVAL:
            return _refresh_cached_feed_status(cached)
        if _yahoo_live_cooldown_active():
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed(YAHOO_LIVE_FEED_STATE.get('error_type') or 'cooldown', YAHOO_LIVE_FEED_STATE.get('error_message') or 'Yahoo live Spot cooldown is active.')
        CANONICAL_XAUUSD_FEED_CACHE['last_request_monotonic'] = now_m

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36', 'Accept': 'application/json', 'Referer': 'https://finance.yahoo.com/'}
    try:
        kwargs = {'timeout': 8}
        if HAS_CURL_CFFI:
            kwargs['impersonate'] = 'chrome124'
        response = curl_requests.get('https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X', params={'interval': '1m', 'range': '1d'}, headers=headers, **kwargs)
    except Exception as exc:
        msg = f'{type(exc).__name__}: {exc}'[:300]
        _set_yahoo_live_cooldown('network', msg)
        _log_yahoo_live_failure('network', None, msg)
        return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('network', msg)

    code = int(getattr(response, 'status_code', 0) or 0)
    if code == 429:
        msg = _yahoo_live_error_message(response) or 'Yahoo live HTTP 429 rate limit.'
        _set_yahoo_live_cooldown('rate_limited', msg)
        _log_yahoo_live_failure('rate_limited', code, msg)
        return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('rate_limited', msg)
    if 500 <= code <= 599:
        msg = _yahoo_live_error_message(response) or f'Yahoo live HTTP {code}.'
        _set_yahoo_live_cooldown('server_error', msg)
        _log_yahoo_live_failure('server_error', code, msg)
        return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('server_error', msg)
    if code != 200:
        msg = _yahoo_live_error_message(response) or f'Yahoo live HTTP {code}.'
        return _build_missing_yahoo_feed('http_error', msg)
    try:
        payload = response.json()
    except Exception as exc:
        msg = f'{type(exc).__name__}: {exc}'[:300]
        _set_yahoo_live_cooldown('malformed_json', msg)
        _log_yahoo_live_failure('malformed_json', code, msg)
        return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('malformed_json', msg)

    feed = _build_yahoo_live_feed(payload, fetched_at=datetime.now(timezone.utc))
    if feed.get('status') == 'MISSING':
        msg = feed.get('error_message') or 'Yahoo XAUUSD=X live payload did not contain a valid quote.'
        _set_yahoo_live_cooldown(feed.get('error_type') or 'invalid_data', msg)
        CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
        _log_yahoo_live_failure(feed.get('error_type'), code, msg)
        return feed
    with YAHOO_LIVE_STATE_LOCK:
        YAHOO_LIVE_FEED_STATE['blocked_until'] = 0.0
        YAHOO_LIVE_FEED_STATE['error_type'] = None
        YAHOO_LIVE_FEED_STATE['error_message'] = None
    CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
    return feed
''')

text = replace_function(text, 'get_chart_data_cached', r'''
def get_chart_data_cached():
    """Historical cache freshness is updated only after at least one gold timeframe succeeds."""
    now = datetime.now(timezone.utc)
    with cache_lock:
        last = MARKET_DATA_CACHE['last_fetch']
        if last is not None and (now - last).total_seconds() < YAHOO_HISTORICAL_FETCH_INTERVAL_SECONDS:
            return MARKET_DATA_CACHE.copy()
    with fetch_lock:
        with cache_lock:
            last = MARKET_DATA_CACHE['last_fetch']
            if last is not None and (now - last).total_seconds() < YAHOO_HISTORICAL_FETCH_INTERVAL_SECONDS:
                return MARKET_DATA_CACHE.copy()
        try:
            df_gold_h1 = fetch_yahoo_direct('XAUUSD=X', range_str='60d', interval_str='1h')
            df_gold_m15 = fetch_yahoo_direct('XAUUSD=X', range_str='10d', interval_str='15m')
            df_dxy_m15 = fetch_yahoo_direct('DX-Y.NYB', range_str='10d', interval_str='15m')
            df_us10y_m15 = fetch_yahoo_direct('^TNX', range_str='10d', interval_str='15m')
            with cache_lock:
                if not df_gold_h1.empty: MARKET_DATA_CACHE['df_gold_h1'] = df_gold_h1
                if not df_gold_m15.empty: MARKET_DATA_CACHE['df_gold_m15'] = df_gold_m15
                if not df_dxy_m15.empty: MARKET_DATA_CACHE['df_dxy_m15'] = df_dxy_m15
                if not df_us10y_m15.empty: MARKET_DATA_CACHE['df_us10y_m15'] = df_us10y_m15
                if not df_gold_h1.empty or not df_gold_m15.empty:
                    MARKET_DATA_CACHE['last_fetch'] = now
        except Exception as exc:
            logger.warning('[MARKET_DATA] historical fetch failure type=%s message=%s', type(exc).__name__, str(exc)[:300])
    with cache_lock:
        return MARKET_DATA_CACHE.copy()
''')

for token in ('GC=F', 'PAXGUSDT', 'ifcmarkets.net', 'METALS_DEV_API_KEY', 'ARGENT_API_KEY', 'api.argentapi.com', 'YAHOO_FEED_STATE', 'YAHOO_STATE_LOCK', 'def _yahoo_cooldown_active'):
    if token.lower() in text.lower():
        raise RuntimeError(f'legacy market-data marker remains: {token}')

if 'XAUUSD=X' not in text:
    raise RuntimeError('canonical XAUUSD=X symbol missing')
if text == original:
    raise RuntimeError('no production change generated')

ast.parse(text)
BOT.write_text(text, encoding='utf-8')
subprocess.run(['python3', '-m', 'py_compile', 'bot.py'], check=True)
subprocess.run(['git', 'diff', '--check'], check=True)

# Phase 1 core regression without importing project dependencies.
tree = ast.parse(text)
needed = {'_trade_direction', 'validate_trade_levels', '_calculate_realized_r', 'evaluate_trade_lifecycle'}
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in needed]
ns = {'np': type('FakeNP', (), {'isfinite': staticmethod(math.isfinite)})}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
assert ns['validate_trade_levels']('BUY', 100, 95, 110, 120)[0]
assert ns['validate_trade_levels']('SELL', 100, 105, 90, 80)[0]
assert not ns['validate_trade_levels']('BUY', 100, 105, 110, 120)[0]
assert math.isclose(ns['_calculate_realized_r']('BUY', 100, 95, 120), 4.0)
assert math.isclose(ns['_calculate_realized_r']('SELL', 100, 105, 80), 4.0)
assert ns['evaluate_trade_lifecycle']('BUY', 'OPEN', 100, 95, 110, 120, price=112) == [('TP1_HIT', 110.0)]
print('YAHOO_SPOT_HARDENING_OK')
print('PHASE1_CORE_REGRESSION_OK')

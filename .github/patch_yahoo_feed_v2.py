from pathlib import Path
import ast
import math
import subprocess
import textwrap

BOT = Path('bot.py')
text = BOT.read_text(encoding='utf-8')
original = text

old_import = """try:\n    from curl_cffi import requests as curl_requests\nexcept ImportError:\n    curl_requests = requests\n"""
if old_import in text:
    text = text.replace(old_import, """try:\n    from curl_cffi import requests as curl_requests\n    HAS_CURL_CFFI = True\nexcept ImportError:\n    curl_requests = requests\n    HAS_CURL_CFFI = False\n""", 1)


def replace_function(src, name, replacement):
    tree = ast.parse(src)
    node = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)
    if node is None:
        raise RuntimeError(f'function not found: {name}')
    lines = src.splitlines(True)
    return ''.join(lines[:node.lineno-1] + [textwrap.dedent(replacement).strip('\n') + '\n\n'] + lines[node.end_lineno:])

text = replace_function(text, 'fetch_yahoo_direct', r'''
def fetch_yahoo_direct(symbol, range_str="10d", interval_str="15m"):
    if _yahoo_historical_cooldown_active():
        return pd.DataFrame()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json,text/html,application/xhtml+xml',
        'Referer': 'https://finance.yahoo.com/'
    }
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
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
            msg = f'Yahoo historical HTTP {status}.'
            if status >= 500:
                _set_yahoo_historical_cooldown('server_error', msg)
            else:
                logger.warning('[MARKET_DATA] Yahoo historical failure type=http_error http_status=%s message=%s', status, msg)
            return pd.DataFrame()
        data = response.json()
        result = (data.get('chart') or {}).get('result') or []
        if not result or not isinstance(result[0], dict):
            _set_yahoo_historical_cooldown('missing_result', 'Yahoo historical response has no chart.result.')
            return pd.DataFrame()
        ts = result[0].get('timestamp') or []
        quotes = (result[0].get('indicators') or {}).get('quote') or []
        if not ts or not quotes:
            _set_yahoo_historical_cooldown('missing_ohlc', f'Yahoo {symbol} {interval_str} returned no OHLC rows.')
            return pd.DataFrame()
        q = quotes[0]
        df = pd.DataFrame({
            'Open': q.get('open', []), 'High': q.get('high', []),
            'Low': q.get('low', []), 'Close': q.get('close', []),
            'Volume': q.get('volume', [0] * len(ts)),
        }, index=pd.to_datetime(ts, unit='s', utc=True)).dropna(subset=['Open','High','Low','Close'])
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
        if '429' in low or 'too many requests' in low or 'rate limit' in low or 'yfratelimiterror' in low:
            _set_yahoo_historical_cooldown('rate_limited', msg)
        elif 'timeout' in low:
            _set_yahoo_historical_cooldown('timeout', msg)
        else:
            _set_yahoo_historical_cooldown('network_or_response', msg)
        return pd.DataFrame()
''')

text = replace_function(text, 'fetch_canonical_xauusd_feed', r'''
def fetch_canonical_xauusd_feed():
    """Canonical live XAUUSD Spot from Yahoo XAUUSD=X only; no futures/crypto/scraping fallback."""
    now_m = time.monotonic()
    with PRICE_FEED_LOCK:
        cached = CANONICAL_XAUUSD_FEED_CACHE.get('feed')
        last = float(CANONICAL_XAUUSD_FEED_CACHE.get('last_request_monotonic', 0.0) or 0.0)
        if now_m - last < PRICE_FEED_MIN_REQUEST_INTERVAL:
            return _refresh_cached_feed_status(cached)
        if _yahoo_live_cooldown_active():
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed(
                YAHOO_LIVE_FEED_STATE.get('error_type') or 'cooldown',
                YAHOO_LIVE_FEED_STATE.get('error_message') or 'Yahoo live Spot cooldown is active.'
            )
        CANONICAL_XAUUSD_FEED_CACHE['last_request_monotonic'] = now_m

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json', 'Referer': 'https://finance.yahoo.com/'
    }
    try:
        kwargs = {'timeout': 8}
        if HAS_CURL_CFFI:
            kwargs['impersonate'] = 'chrome124'
        response = curl_requests.get(
            'https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X',
            params={'interval': '1m', 'range': '1d'},
            headers=headers,
            **kwargs
        )
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
        feed = _build_missing_yahoo_feed('http_error', msg)
        CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
        _log_yahoo_live_failure('http_error', code, msg)
        return feed
    try:
        payload = response.json()
    except Exception as exc:
        msg = f'{type(exc).__name__}: {exc}'[:300]
        _set_yahoo_live_cooldown('malformed_json', msg)
        _log_yahoo_live_failure('malformed_json', code, msg)
        return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('malformed_json', msg)
    feed = _build_yahoo_live_feed(payload, fetched_at=datetime.now(timezone.utc))
    if feed.get('status') == 'MISSING':
        msg = feed.get('error_message') or 'Yahoo XAUUSD=X live response did not contain a valid quote.'
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

text = text.replace("""        if _yahoo_historical_cooldown_active():\n            with cache_lock: return MARKET_DATA_CACHE.copy()\n""", '', 1)
text = text.replace("""                MARKET_DATA_CACHE[\"last_fetch\"]=now""", """                if not df_gold_h1.empty or not df_gold_m15.empty:\n                    MARKET_DATA_CACHE[\"last_fetch\"]=now""", 1)
text = text.replace("""        except Exception as exc:\n            logger.warning('[MARKET_DATA] historical fetch failure type=%s message=%s',type(exc).__name__,str(exc)[:300])\n            with cache_lock: MARKET_DATA_CACHE[\"last_fetch\"]=now\n""", """        except Exception as exc:\n            logger.warning('[MARKET_DATA] historical fetch failure type=%s message=%s',type(exc).__name__,str(exc)[:300])\n""", 1)

text = text.replace("    reason = feed.get('error_message') if feed.get('status') == 'MISSING' else None", "    reason = (feed.get('error_message') or feed.get('error_type') or 'Unknown Yahoo Spot feed failure') if feed.get('status') == 'MISSING' else None", 1)

for forbidden in ('GC=F','PAXGUSDT','ifcmarkets.net','METALS_DEV_API_KEY','ARGENT_API_KEY','api.argentapi.com'):
    if forbidden.lower() in text.lower():
        raise RuntimeError(f'forbidden source remains: {forbidden}')

if text == original:
    raise RuntimeError('no production change was generated')
ast.parse(text)
BOT.write_text(text, encoding='utf-8')
subprocess.run(['python3','-m','py_compile','bot.py'], check=True)
subprocess.run(['git','diff','--check'], check=True)

# Phase 1 regression without importing project dependencies.
tree = ast.parse(text)
needed = {'_trade_direction','validate_trade_levels','_calculate_realized_r','evaluate_trade_lifecycle'}
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in needed]
ns = {'np': type('FakeNP', (), {'isfinite': staticmethod(math.isfinite)}), 'math': math}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
v=ns['validate_trade_levels']; r=ns['_calculate_realized_r']; e=ns['evaluate_trade_lifecycle']
assert v('BUY',100,95,110,120)[0] and v('SELL',100,105,90,80)[0]
assert not v('BUY',100,105,110,120)[0] and not v('SELL',100,95,90,80)[0]
assert math.isclose(r('BUY',100,95,120),4.0) and math.isclose(r('SELL',100,105,80),4.0)
assert e('BUY','OPEN',100,95,110,120,price=112)==[('TP1_HIT',110.0)]
assert e('BUY','TP1_HIT',100,95,110,120,price=121)==[('TP2_HIT',121.0)]
assert e('SELL','OPEN',100,105,90,80,price=91)==[('TP1_HIT',90.0)]
assert e('SELL','TP1_HIT',100,105,90,80,price=79)==[('TP2_HIT',79.0)]
print('YAHOO_SPOT_HARDENING_OK')
print('PHASE1_REGRESSION_OK')

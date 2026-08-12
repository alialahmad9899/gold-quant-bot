from pathlib import Path
import ast
import asyncio
import math
import re
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta

BOT = Path('bot.py')
WORKFLOW = Path('.github/workflows/codex_market_feed_fix.yml')
SCRIPT = Path('.github/codex_market_feed_fix.py')


def replace_block(text, start, end, replacement):
    a = text.index(start)
    b = text.index(end, a)
    return text[:a] + replacement.rstrip() + '\n\n' + text[b:]


def patch_bot():
    s = BOT.read_text(encoding='utf-8')

    old = "CANONICAL_XAUUSD_FEED_CACHE = {'feed': None, 'last_request_monotonic': 0.0}\n"
    new = old + """ARGENT_AUTH_COOLDOWN_SECONDS = int(os.getenv('ARGENT_AUTH_COOLDOWN_SECONDS', '600'))
ARGENT_TRANSIENT_COOLDOWN_SECONDS = int(os.getenv('ARGENT_TRANSIENT_COOLDOWN_SECONDS', '300'))
ARGENT_FEED_STATE = {'blocked_until': 0.0, 'error_type': None, 'error_message': None}
YAHOO_COOLDOWN_SECONDS = int(os.getenv('YAHOO_COOLDOWN_SECONDS', '1800'))
YAHOO_FETCH_INTERVAL_SECONDS = int(os.getenv('YAHOO_FETCH_INTERVAL_SECONDS', '900'))
YAHOO_FEED_STATE = {'blocked_until': 0.0, 'error_type': None, 'error_message': None}
YAHOO_STATE_LOCK = threading.Lock()
"""
    if old not in s:
        raise RuntimeError('feed-state anchor not found')
    s = s.replace(old, new, 1)

    # yfinance is intentionally no longer used; Yahoo is historical-only via the direct chart API.
    s = s.replace('import yfinance as yf\n', '', 1)

    argent_helpers = '''def _sanitize_feed_error(message):
    text = str(message or '')[:300]
    if ARGENT_API_KEY:
        text = text.replace(ARGENT_API_KEY, '[REDACTED]')
    return text


def _set_argent_cooldown(seconds, error_type=None, error_message=None):
    ARGENT_FEED_STATE['blocked_until'] = time.monotonic() + max(0.0, float(seconds))
    ARGENT_FEED_STATE['error_type'] = error_type
    ARGENT_FEED_STATE['error_message'] = _sanitize_feed_error(error_message)


def _argent_cooldown_active():
    return time.monotonic() < float(ARGENT_FEED_STATE.get('blocked_until', 0.0) or 0.0)


def _build_missing_argent_feed(error_type='missing', error_message='No live XAUUSD data available.'):
    now = datetime.now(timezone.utc)
    return {
        'symbol': 'XAUUSD', 'provider': 'ArgentAPI', 'status': 'MISSING',
        'bid': None, 'ask': None, 'mid': None, 'spot': None,
        'source_timestamp': None, 'timestamp': None,
        'fetched_timestamp': now.isoformat(), 'source_fetched_timestamp': None,
        'age_seconds': None, 'error_type': str(error_type),
        'error_message': _sanitize_feed_error(error_message),
    }


def _argentapi_error_message(response):
    try:
        payload = response.json()
        if isinstance(payload, dict):
            for key in ('error', 'message', 'detail'):
                if payload.get(key):
                    return _sanitize_feed_error(payload[key])
    except Exception:
        pass
    try:
        return _sanitize_feed_error(response.text or '')
    except Exception:
        return ''


def _log_argentapi_failure(error_type, http_status=None, message=''):
    logger.warning('[XAUUSD_FEED] ArgentAPI failure type=%s http_status=%s message=%s', error_type, http_status, _sanitize_feed_error(message))
'''
    s = replace_block(s, 'def _argentapi_error_message(response):', 'def fetch_canonical_xauusd_feed():', argent_helpers)

    argent_fetch = '''def fetch_canonical_xauusd_feed():
    """Canonical live XAUUSD feed backed only by ArgentAPI with a 60s request gate."""
    now_monotonic = time.monotonic()
    with PRICE_FEED_LOCK:
        cached = CANONICAL_XAUUSD_FEED_CACHE.get('feed')
        last_request = float(CANONICAL_XAUUSD_FEED_CACHE.get('last_request_monotonic', 0.0) or 0.0)
        if now_monotonic - last_request < PRICE_FEED_MIN_REQUEST_INTERVAL:
            return _refresh_cached_feed_status(cached)

        if _argent_cooldown_active():
            if cached:
                return _refresh_cached_feed_status(cached)
            return _build_missing_argent_feed(
                ARGENT_FEED_STATE.get('error_type') or 'cooldown',
                ARGENT_FEED_STATE.get('error_message') or 'ArgentAPI request cooldown is active.'
            )

        if not ARGENT_API_KEY:
            feed = _build_missing_argent_feed('missing_api_key', 'ARGENT_API_KEY is not configured.')
            _set_argent_cooldown(ARGENT_AUTH_COOLDOWN_SECONDS, 'missing_api_key', feed['error_message'])
            CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
            _log_argentapi_failure(feed['error_type'], None, feed['error_message'])
            return feed

        CANONICAL_XAUUSD_FEED_CACHE['last_request_monotonic'] = now_monotonic
        try:
            response = requests.get(
                'https://api.argentapi.com/v1/spot/gold',
                headers={'X-API-Key': ARGENT_API_KEY, 'Accept': 'application/json'},
                timeout=5,
            )
        except requests.Timeout as exc:
            message = _sanitize_feed_error(exc)
            _set_argent_cooldown(ARGENT_TRANSIENT_COOLDOWN_SECONDS, 'timeout', message)
            _log_argentapi_failure('timeout', None, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_argent_feed('timeout', message)
        except requests.RequestException as exc:
            message = _sanitize_feed_error(exc)
            _set_argent_cooldown(ARGENT_TRANSIENT_COOLDOWN_SECONDS, 'network', message)
            _log_argentapi_failure('network', None, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_argent_feed('network', message)
        except Exception as exc:
            message = _sanitize_feed_error(exc)
            _set_argent_cooldown(ARGENT_TRANSIENT_COOLDOWN_SECONDS, 'network', message)
            _log_argentapi_failure('network', None, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_argent_feed('network', message)

        status_code = int(getattr(response, 'status_code', 0) or 0)
        if status_code in (401, 403):
            message = _argentapi_error_message(response) or 'ArgentAPI authentication/authorization failed.'
            _set_argent_cooldown(ARGENT_AUTH_COOLDOWN_SECONDS, 'authentication', message)
            feed = _build_missing_argent_feed('authentication', message)
            CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
            _log_argentapi_failure('authentication', status_code, message)
            return feed

        if status_code == 429:
            message = _argentapi_error_message(response) or 'ArgentAPI rate limit reached.'
            _set_argent_cooldown(ARGENT_TRANSIENT_COOLDOWN_SECONDS, 'rate_limited', message)
            _log_argentapi_failure('rate_limited', status_code, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_argent_feed('rate_limited', message)

        if 500 <= status_code <= 599:
            message = _argentapi_error_message(response) or 'ArgentAPI server error.'
            _set_argent_cooldown(ARGENT_TRANSIENT_COOLDOWN_SECONDS, 'server_error', message)
            _log_argentapi_failure('server_error', status_code, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_argent_feed('server_error', message)

        if status_code != 200:
            message = _argentapi_error_message(response) or f'Unexpected HTTP status {status_code}.'
            _set_argent_cooldown(ARGENT_TRANSIENT_COOLDOWN_SECONDS, 'http_error', message)
            _log_argentapi_failure('http_error', status_code, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_argent_feed('http_error', message)

        try:
            payload = response.json()
        except Exception as exc:
            message = _sanitize_feed_error(exc)
            _set_argent_cooldown(ARGENT_TRANSIENT_COOLDOWN_SECONDS, 'malformed_json', message)
            _log_argentapi_failure('malformed_json', status_code, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_argent_feed('malformed_json', message)

        feed = _build_canonical_xauusd_feed(payload, fetched_at=datetime.now(timezone.utc))
        feed['error_type'] = None
        feed['error_message'] = None
        if feed.get('status') != 'MISSING':
            ARGENT_FEED_STATE['blocked_until'] = 0.0
            ARGENT_FEED_STATE['error_type'] = None
            ARGENT_FEED_STATE['error_message'] = None
            CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
            return feed

        message = 'required live price/timestamp fields are missing or invalid.'
        _set_argent_cooldown(ARGENT_TRANSIENT_COOLDOWN_SECONDS, 'missing_fields', message)
        feed = _build_missing_argent_feed('missing_fields', message)
        CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
        _log_argentapi_failure('missing_fields', status_code, message)
        return feed
'''
    s = replace_block(s, 'def fetch_canonical_xauusd_feed():', 'def get_xauusd_execution_price', argent_fetch)

    # Add explicit timestamp + error fields to canonical feed dictionaries.
    s = s.replace("        'source_timestamp': source_ts.isoformat(),\n        'fetched_timestamp': now.isoformat(),", "        'source_timestamp': source_ts.isoformat(),\n        'timestamp': source_ts.isoformat(),\n        'fetched_timestamp': now.isoformat(),", 1)
    s = s.replace("            'source_timestamp': None, 'fetched_timestamp': now.isoformat(),\n            'source_fetched_timestamp': None, 'age_seconds': None,\n        }", "            'source_timestamp': None, 'timestamp': None, 'fetched_timestamp': now.isoformat(),\n            'source_fetched_timestamp': None, 'age_seconds': None, 'error_type': 'missing', 'error_message': 'No live XAUUSD data available.',\n        }", 1)
    s = s.replace("            'source_timestamp': source_ts.isoformat() if source_ts else None,\n            'fetched_timestamp': now.isoformat(),", "            'source_timestamp': source_ts.isoformat() if source_ts else None,\n            'timestamp': source_ts.isoformat() if source_ts else None,\n            'fetched_timestamp': now.isoformat(),", 1)

    yahoo_helpers = '''def _set_yahoo_cooldown(error_type, error_message):
    with YAHOO_STATE_LOCK:
        YAHOO_FEED_STATE['blocked_until'] = time.monotonic() + max(1, YAHOO_COOLDOWN_SECONDS)
        YAHOO_FEED_STATE['error_type'] = str(error_type)
        YAHOO_FEED_STATE['error_message'] = str(error_message or '')[:300]
    logger.warning('[MARKET_DATA] Yahoo cooldown type=%s seconds=%s message=%s', error_type, YAHOO_COOLDOWN_SECONDS, str(error_message or '')[:300])


def _yahoo_cooldown_active():
    with YAHOO_STATE_LOCK:
        return time.monotonic() < float(YAHOO_FEED_STATE.get('blocked_until', 0.0) or 0.0)


def fetch_yahoo_direct(symbol, range_str="10d", interval_str="15m"):
    if _yahoo_cooldown_active():
        return pd.DataFrame()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json,text/html,application/xhtml+xml',
        'Referer': 'https://finance.yahoo.com/'
    }
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval_str}&range={range_str}"
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 429:
            message = 'Yahoo HTTP 429 rate limit.'
            _set_yahoo_cooldown('rate_limited', message)
            return pd.DataFrame()
        if response.status_code != 200:
            if response.status_code >= 500:
                _set_yahoo_cooldown('server_error', f'Yahoo HTTP {response.status_code}')
            return pd.DataFrame()
        data = response.json()
        result = data['chart']['result'][0]
        timestamps = result['timestamp']
        quote = result['indicators']['quote'][0]
        df = pd.DataFrame({
            'Open': quote['open'], 'High': quote['high'], 'Low': quote['low'],
            'Close': quote['close'], 'Volume': quote.get('volume', [0] * len(timestamps))
        }, index=pd.to_datetime(timestamps, unit='s', utc=True))
        df = df.dropna(subset=['Close'])
        return df if not df.empty else pd.DataFrame()
    except Exception as exc:
        error_text = f'{type(exc).__name__}: {exc}'
        if any(token in error_text.lower() for token in ('yfratelimiterror', 'too many requests', 'http 429', '429')):
            _set_yahoo_cooldown('rate_limited', error_text)
        return pd.DataFrame()
'''
    s = replace_block(s, 'def fetch_yahoo_direct(symbol, range_str="10d", interval_str="15m"):', 'def _parse_price_feed_timestamp', yahoo_helpers)

    chart = '''def get_chart_data_cached():
    now = datetime.now(timezone.utc)
    with cache_lock:
        last = MARKET_DATA_CACHE["last_fetch"]
        if last is not None and (now - last).total_seconds() < YAHOO_FETCH_INTERVAL_SECONDS:
            return MARKET_DATA_CACHE.copy()

    with fetch_lock:
        with cache_lock:
            last = MARKET_DATA_CACHE["last_fetch"]
            if last is not None and (now - last).total_seconds() < YAHOO_FETCH_INTERVAL_SECONDS:
                return MARKET_DATA_CACHE.copy()
        if _yahoo_cooldown_active():
            with cache_lock:
                return MARKET_DATA_CACHE.copy()
        try:
            df_gold_h1 = fetch_yahoo_direct("XAUUSD=X", range_str="60d", interval_str="1h")
            df_gold_m15 = fetch_yahoo_direct("XAUUSD=X", range_str="10d", interval_str="15m")
            df_dxy_m15 = fetch_yahoo_direct("DX-Y.NYB", range_str="10d", interval_str="15m")
            df_us10y_m15 = fetch_yahoo_direct("^TNX", range_str="10d", interval_str="15m")
            with cache_lock:
                if not df_gold_h1.empty:
                    MARKET_DATA_CACHE["df_gold_h1"] = df_gold_h1
                if not df_gold_m15.empty:
                    MARKET_DATA_CACHE["df_gold_m15"] = df_gold_m15
                if not df_dxy_m15.empty:
                    MARKET_DATA_CACHE["df_dxy_m15"] = df_dxy_m15
                if not df_us10y_m15.empty:
                    MARKET_DATA_CACHE["df_us10y_m15"] = df_us10y_m15
                MARKET_DATA_CACHE["last_fetch"] = now
        except Exception as exc:
            logger.warning('[MARKET_DATA] historical fetch failure type=%s message=%s', type(exc).__name__, str(exc)[:300])
            with cache_lock:
                MARKET_DATA_CACHE["last_fetch"] = now
    with cache_lock:
        return MARKET_DATA_CACHE.copy()
'''
    s = replace_block(s, 'def get_chart_data_cached():', 'def get_verified_closed_m15', chart)

    # Background loop must not hit Yahoo for DXY/TNX every 5 seconds.
    old_cache_fetch = '''        headers = {'User-Agent': 'Mozilla/5.0'}
        dxy = 99.85
        us10y = 4.63
        try:
            r_dxy = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1m&range=1d", headers=headers, timeout=2)
            if r_dxy.status_code == 200:
                dxy = float(r_dxy.json()['chart']['result'][0]['meta']['regularMarketPrice'])
        except Exception:
            pass
        try:
            r_tnx = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/^TNX?interval=1m&range=1d", headers=headers, timeout=2)
            if r_tnx.status_code == 200:
                us10y = float(r_tnx.json()['chart']['result'][0]['meta']['regularMarketPrice'])
        except Exception:
            pass
        with cache_lock:
            GLOBAL_CACHE['market_data'] = {
                'gold': round(float(gold), 2) if gold else 0.0,
                'dxy': round(float(dxy), 2),
                'us10y': round(float(us10y), 2),
                'price_feed': feed,
            }
'''
    new_cache_fetch = '''        with cache_lock:
            previous = GLOBAL_CACHE.get('market_data') or {}
            GLOBAL_CACHE['market_data'] = {
                'gold': round(float(gold), 2) if gold else 0.0,
                'dxy': previous.get('dxy'),
                'us10y': previous.get('us10y'),
                'price_feed': feed,
            }
'''
    if old_cache_fetch not in s:
        raise RuntimeError('background Yahoo fetch block not found')
    s = s.replace(old_cache_fetch, new_cache_fetch, 1)

    # /price exposes source timestamp and explicit error reason while keeping unavailable prices as N/A.
    old_price = '''    msg = (
        "📊 **XAUUSD Live Price Feed**\\n"
        f"Provider: {feed.get('provider', 'ArgentAPI')}\\n"
        f"Status: {feed.get('status', 'MISSING')}\\n"
        f"Bid: {fmt(feed.get('bid'))}\\n"
        f"Ask: {fmt(feed.get('ask'))}\\n"
        f"Mid: {fmt(feed.get('mid'))}\\n"
        f"Age: {age}\\n"
        f"💵 مؤشر الدولار: {data['dxy']}\\n"
        f"📈 عوائد السندات: {data['us10y']}%"
    )'''
    new_price = '''    reason = feed.get('error_message') if feed.get('status') == 'MISSING' else None
    timestamp = feed.get('timestamp') or feed.get('source_timestamp') or 'N/A'
    msg = (
        "📊 **XAUUSD Live Price Feed**\\n"
        f"Provider: {feed.get('provider', 'ArgentAPI')}\\n"
        f"Status: {feed.get('status', 'MISSING')}\\n"
        f"Bid: {fmt(feed.get('bid'))}\\n"
        f"Ask: {fmt(feed.get('ask'))}\\n"
        f"Mid: {fmt(feed.get('mid'))}\\n"
        f"Timestamp: {timestamp}\\n"
        f"Age: {age}\\n"
        f"Reason: {reason or 'N/A'}\\n"
        f"💵 مؤشر الدولار: {data['dxy'] if data.get('dxy') is not None else 'N/A'}\\n"
        f"📈 عوائد السندات: {f\"{data['us10y']:.2f}%\" if data.get('us10y') is not None else 'N/A'}"
    )'''
    if old_price not in s:
        raise RuntimeError('/price block not found')
    s = s.replace(old_price, new_price, 1)

    # Strong source guards.
    for forbidden in ('PAXGUSDT', 'GC=F', 'ifcmarkets.net', 'IFC Markets', 'metals.dev'):
        if forbidden.lower() in s.lower():
            raise RuntimeError(f'forbidden source remains: {forbidden}')
    if 'yf.download(' in s or 'import yfinance as yf' in s:
        raise RuntimeError('yfinance fallback remains')
    if 'https://api.argentapi.com/v1/spot/gold' not in s or "headers={'X-API-Key': ARGENT_API_KEY" not in s:
        raise RuntimeError('ArgentAPI canonical request missing')
    BOT.write_text(s, encoding='utf-8')


def run_tests():
    subprocess.run(['python3', '-m', 'py_compile', 'bot.py'], check=True)
    subprocess.run(['git', 'diff', '--check'], check=True)

    text = BOT.read_text(encoding='utf-8')
    tree = ast.parse(text)
    wanted = {
        '_parse_price_feed_timestamp','_finite_positive','_build_canonical_xauusd_feed','_refresh_cached_feed_status',
        '_sanitize_feed_error','_set_argent_cooldown','_argent_cooldown_active','_build_missing_argent_feed',
        '_argentapi_error_message','_log_argentapi_failure','fetch_canonical_xauusd_feed','get_xauusd_execution_price',
        '_set_yahoo_cooldown','_yahoo_cooldown_active','fetch_yahoo_direct','get_chart_data_cached',
        '_trade_direction','validate_trade_levels','_calculate_realized_r','evaluate_trade_lifecycle'
    }
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]

    class FakeLogger:
        def __init__(self): self.messages=[]
        def warning(self,*args): self.messages.append(args)
        def error(self,*args): self.messages.append(args)

    class FakeTimeout(Exception): pass
    class FakeRequestException(Exception): pass

    class Resp:
        def __init__(self,status,payload=None,text=''):
            self.status_code=status; self._payload=payload; self.text=text
        def json(self):
            if isinstance(self._payload, BaseException): raise self._payload
            return self._payload

    class FakeRequests:
        Timeout = FakeTimeout
        RequestException = FakeRequestException
        def __init__(self, responses): self.responses=list(responses); self.calls=0; self.seen=[]
        def get(self, url, headers=None, timeout=None):
            self.calls += 1; self.seen.append((url,dict(headers or {}),timeout))
            item=self.responses.pop(0)
            if isinstance(item, BaseException): raise item
            return item

    class FakeNP:
        isfinite = staticmethod(math.isfinite)

    now = datetime.now(timezone.utc)
    base = {
        'bid':4395.0,'ask':4397.0,'mid':4396.0,'price':4396.0,
        'sourceTimestamp':now.isoformat(),'fetchedAt':int(now.timestamp()*1000),'ageMs':10000,'stale':False
    }
    ns = {
        'np':FakeNP,'math':math,'datetime':datetime,'timezone':timezone,'timedelta':timedelta,
        'time':time,'threading':threading,'logger':FakeLogger(),'ARGENT_API_KEY':'test-key',
        'PRICE_FEED_STALE_SECONDS':90,'PRICE_FEED_MIN_REQUEST_INTERVAL':60.0,
        'ARGENT_AUTH_COOLDOWN_SECONDS':600,'ARGENT_TRANSIENT_COOLDOWN_SECONDS':300,
        'ARGENT_FEED_STATE':{'blocked_until':0.0,'error_type':None,'error_message':None},
        'PRICE_FEED_LOCK':threading.Lock(),'CANONICAL_XAUUSD_FEED_CACHE':{'feed':None,'last_request_monotonic':0.0},
        'requests':None,'pd':__import__('pandas'),'YAHOO_COOLDOWN_SECONDS':1800,
        'YAHOO_FETCH_INTERVAL_SECONDS':900,'YAHOO_FEED_STATE':{'blocked_until':0.0,'error_type':None,'error_message':None},
        'YAHOO_STATE_LOCK':threading.Lock()
    }
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'bot.py','exec'),ns)

    build=ns['_build_canonical_xauusd_feed']; refresh=ns['_refresh_cached_feed_status']; fetch=ns['fetch_canonical_xauusd_feed']; exec_price=ns['get_xauusd_execution_price']
    feed=build(base,fetched_at=now)
    assert feed['status']=='ACTIVE' and feed['provider']=='ArgentAPI' and feed['symbol']=='XAUUSD'
    assert feed['mid']==4396.0 and feed['timestamp']==feed['source_timestamp']
    assert exec_price(feed,'BUY','ENTRY')==4397.0 and exec_price(feed,'SELL','ENTRY')==4395.0
    assert exec_price(feed,'BUY','EXIT')==4395.0 and exec_price(feed,'SELL','EXIT')==4397.0
    stale=build(dict(base,ageMs=120000,sourceTimestamp=(now-timedelta(seconds=120)).isoformat()),fetched_at=now)
    assert stale['status']=='STALE' and exec_price(stale,'BUY','ENTRY') is None
    assert build({'sourceTimestamp':now.isoformat()},fetched_at=now)['status']=='MISSING'

    def run_fetch(response,key='test-key'):
        ns['CANONICAL_XAUUSD_FEED_CACHE']={'feed':None,'last_request_monotonic':0.0}
        ns['ARGENT_FEED_STATE']={'blocked_until':0.0,'error_type':None,'error_message':None}
        ns['ARGENT_API_KEY']=key
        fake=FakeRequests([response]); ns['requests']=fake
        return fetch(),fake

    result,fake=run_fetch(Resp(200,base))
    assert result['status']=='ACTIVE' and fake.calls==1
    assert fake.seen[0][0]=='https://api.argentapi.com/v1/spot/gold' and fake.seen[0][1]['X-API-Key']=='test-key'
    assert 'test-key' not in fake.seen[0][0]
    assert fetch()['status']=='ACTIVE' and fake.calls==1
    for code in (401,403):
        result,fake=run_fetch(Resp(code,{'message':'invalid_key'})); assert result['status']=='MISSING' and result['error_type']=='authentication'
        assert fetch()['status']=='MISSING' and fake.calls==1
    for code,etype in ((429,'rate_limited'),(503,'server_error')):
        result,fake=run_fetch(Resp(code,{'message':'provider failure'})); assert result['status']=='MISSING' and result['error_type']==etype
    result,fake=run_fetch(FakeTimeout('timeout')); assert result['status']=='MISSING' and result['error_type']=='timeout'
    result,fake=run_fetch(Resp(200,ValueError('bad json'))); assert result['status']=='MISSING' and result['error_type']=='malformed_json'
    result,fake=run_fetch(Resp(200,base),key=''); assert result['status']=='MISSING' and fake.calls==0

    validate=ns['validate_trade_levels']; calc_r=ns['_calculate_realized_r']; lifecycle=ns['evaluate_trade_lifecycle']
    assert validate('BUY',100,95,110,120)[0] and validate('SELL',100,105,90,80)[0]
    assert not validate('BUY',100,105,110,120)[0] and not validate('SELL',100,95,90,80)[0]
    assert abs(calc_r('BUY',100,95,110)-2.0)<1e-9 and abs(calc_r('BUY',100,95,95)+1.0)<1e-9
    assert abs(calc_r('SELL',100,105,90)-2.0)<1e-9 and abs(calc_r('SELL',100,105,105)+1.0)<1e-9
    assert lifecycle('BUY','OPEN',100,95,110,120,price=111)==[('TP1_HIT',110.0)]
    assert lifecycle('BUY','TP1_HIT',100,95,110,120,price=121)==[('TP2_HIT',121.0)]
    assert lifecycle('SELL','OPEN',100,105,90,80,price=89)==[('TP1_HIT',90.0)]
    assert lifecycle('SELL','TP1_HIT',100,105,90,80,price=79)==[('TP2_HIT',79.0)]

    src=BOT.read_text(encoding='utf-8')
    assert 'trade_outcome:{trade_id}' in src
    assert "enqueue_learning_event('TRADE_OUTCOME'" in src
    assert 'evaluate_trade_lifecycle(sig_type,current_status,entry,sl,tp1,tp2' in src
    assert 'slippage=NULL' in src
    assert 'yf.download(' not in src and 'import yfinance as yf' not in src
    for forbidden in ('PAXGUSDT','GC=F','ifcmarkets.net','IFC Markets','metals.dev'):
        assert forbidden.lower() not in src.lower()
    assert re.search(r"fetch_and_update_cache\(\).*?return", src, re.S) is not None
    print('MARKET_FEED_AND_PHASE1_TESTS_OK')


if __name__ == '__main__':
    patch_bot()
    run_tests()
    print('--- FINAL DIFF ---')
    subprocess.run(['git','diff','--','bot.py'], check=True)
    subprocess.run(['git','diff','--stat','--','bot.py'], check=True)

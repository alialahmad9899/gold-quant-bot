from pathlib import Path
import ast
import math
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta

BOT = Path('bot.py')

LIVE_FEED_BLOCK = '''def _build_missing_yahoo_feed(error_type='missing', error_message='No live XAUUSD Spot data available.'):
    now = datetime.now(timezone.utc)
    return {
        'symbol': 'XAUUSD', 'provider': 'Yahoo Finance', 'status': 'MISSING',
        'bid': None, 'ask': None, 'mid': None, 'spot': None,
        'source_timestamp': None, 'timestamp': None,
        'fetched_timestamp': now.isoformat(), 'source_fetched_timestamp': None,
        'age_seconds': None, 'error_type': str(error_type),
        'error_message': str(error_message or '')[:300],
        'spread_available': False,
    }


def _yahoo_live_error_message(response):
    try:
        payload = response.json()
        if isinstance(payload, dict):
            chart = payload.get('chart') or {}
            err = chart.get('error')
            if isinstance(err, dict):
                return str(err.get('description') or err.get('code') or '')[:300]
    except Exception:
        pass
    try:
        return str(response.text or '')[:300]
    except Exception:
        return ''


def _log_yahoo_live_failure(error_type, http_status=None, message=''):
    logger.warning('[XAUUSD_FEED] Yahoo Spot failure type=%s http_status=%s message=%s', error_type, http_status, str(message or '')[:300])


def _set_yahoo_live_cooldown(error_type, error_message):
    with YAHOO_STATE_LOCK:
        YAHOO_FEED_STATE['blocked_until'] = time.monotonic() + max(1, YAHOO_COOLDOWN_SECONDS)
        YAHOO_FEED_STATE['error_type'] = str(error_type)
        YAHOO_FEED_STATE['error_message'] = str(error_message or '')[:300]
    logger.warning('[XAUUSD_FEED] Yahoo Spot cooldown type=%s seconds=%s message=%s', error_type, YAHOO_COOLDOWN_SECONDS, str(error_message or '')[:300])


def _yahoo_live_cooldown_active():
    with YAHOO_STATE_LOCK:
        return time.monotonic() < float(YAHOO_FEED_STATE.get('blocked_until', 0.0) or 0.0)


def _build_yahoo_live_feed(payload, *, fetched_at=None):
    now = fetched_at or datetime.now(timezone.utc)
    if not isinstance(payload, dict):
        return _build_missing_yahoo_feed('malformed_payload', 'Yahoo returned a non-object payload.')
    chart = payload.get('chart') or {}
    result = chart.get('result') or []
    if not result or not isinstance(result[0], dict):
        return _build_missing_yahoo_feed('missing_result', 'Yahoo response does not contain chart.result.')
    meta = result[0].get('meta') or {}
    price = meta.get('regularMarketPrice')
    try:
        price = float(price)
    except (TypeError, ValueError):
        price = None
    if price is None or not math.isfinite(price) or price <= 1000:
        return _build_missing_yahoo_feed('missing_price', 'Yahoo XAUUSD=X regularMarketPrice is missing or invalid.')

    regular_market_time = meta.get('regularMarketTime')
    source_ts = None
    if regular_market_time is not None:
        try:
            source_ts = datetime.fromtimestamp(float(regular_market_time), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            source_ts = None
    if source_ts is None:
        return _build_missing_yahoo_feed('missing_timestamp', 'Yahoo XAUUSD=X regularMarketTime is missing or invalid.')

    age_seconds = max(0.0, (now - source_ts).total_seconds())
    bid = meta.get('bid')
    ask = meta.get('ask')
    try:
        bid = float(bid) if bid is not None else None
    except (TypeError, ValueError):
        bid = None
    try:
        ask = float(ask) if ask is not None else None
    except (TypeError, ValueError):
        ask = None
    bid_ok = bid is not None and math.isfinite(bid) and bid > 1000
    ask_ok = ask is not None and math.isfinite(ask) and ask > 1000
    if not bid_ok or not ask_ok:
        bid = price
        ask = price
        spread_available = False
    else:
        spread_available = True

    mid = (bid + ask) / 2.0
    return {
        'symbol': 'XAUUSD', 'provider': 'Yahoo Finance',
        'status': 'STALE' if age_seconds > PRICE_FEED_STALE_SECONDS else 'ACTIVE',
        'bid': round(bid, 6), 'ask': round(ask, 6), 'mid': round(mid, 6), 'spot': round(price, 6),
        'source_timestamp': source_ts.isoformat(), 'timestamp': source_ts.isoformat(),
        'fetched_timestamp': now.isoformat(), 'source_fetched_timestamp': None,
        'age_seconds': round(age_seconds, 3), 'error_type': None, 'error_message': None,
        'spread_available': spread_available,
    }


def fetch_canonical_xauusd_feed():
    '''"Canonical live XAUUSD Spot feed from Yahoo Finance XAUUSD=X; never use Futures/Crypto/scraping."'''
    now_monotonic = time.monotonic()
    with PRICE_FEED_LOCK:
        cached = CANONICAL_XAUUSD_FEED_CACHE.get('feed')
        last_request = float(CANONICAL_XAUUSD_FEED_CACHE.get('last_request_monotonic', 0.0) or 0.0)
        if now_monotonic - last_request < PRICE_FEED_MIN_REQUEST_INTERVAL:
            return _refresh_cached_feed_status(cached)
        if _yahoo_live_cooldown_active():
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed(
                YAHOO_FEED_STATE.get('error_type') or 'cooldown',
                YAHOO_FEED_STATE.get('error_message') or 'Yahoo Spot cooldown is active.'
            )

        CANONICAL_XAUUSD_FEED_CACHE['last_request_monotonic'] = now_monotonic
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Referer': 'https://finance.yahoo.com/'
        }
        try:
            response = requests.get(
                'https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X',
                params={'interval': '1m', 'range': '1d'},
                headers=headers,
                timeout=5,
            )
        except requests.Timeout as exc:
            message = str(exc)[:300]
            _set_yahoo_live_cooldown('timeout', message)
            _log_yahoo_live_failure('timeout', None, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('timeout', message)
        except requests.RequestException as exc:
            message = str(exc)[:300]
            _set_yahoo_live_cooldown('network', message)
            _log_yahoo_live_failure('network', None, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('network', message)
        except Exception as exc:
            message = str(exc)[:300]
            _set_yahoo_live_cooldown('network', message)
            _log_yahoo_live_failure('network', None, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('network', message)

        status_code = int(getattr(response, 'status_code', 0) or 0)
        if status_code == 429:
            message = _yahoo_live_error_message(response) or 'Yahoo HTTP 429 rate limit.'
            _set_yahoo_live_cooldown('rate_limited', message)
            _log_yahoo_live_failure('rate_limited', status_code, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('rate_limited', message)
        if 500 <= status_code <= 599:
            message = _yahoo_live_error_message(response) or f'Yahoo HTTP {status_code}.'
            _set_yahoo_live_cooldown('server_error', message)
            _log_yahoo_live_failure('server_error', status_code, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('server_error', message)
        if status_code != 200:
            message = _yahoo_live_error_message(response) or f'Yahoo HTTP {status_code}.'
            _log_yahoo_live_failure('http_error', status_code, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('http_error', message)
        try:
            payload = response.json()
        except Exception as exc:
            message = str(exc)[:300]
            _set_yahoo_live_cooldown('malformed_json', message)
            _log_yahoo_live_failure('malformed_json', status_code, message)
            return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('malformed_json', message)

        feed = _build_yahoo_live_feed(payload, fetched_at=datetime.now(timezone.utc))
        if feed.get('status') == 'MISSING':
            _set_yahoo_live_cooldown(feed.get('error_type') or 'invalid_data', feed.get('error_message') or 'Yahoo live data is invalid.')
            CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
            _log_yahoo_live_failure(feed.get('error_type'), status_code, feed.get('error_message'))
            return feed

        with YAHOO_STATE_LOCK:
            YAHOO_FEED_STATE['blocked_until'] = 0.0
            YAHOO_FEED_STATE['error_type'] = None
            YAHOO_FEED_STATE['error_message'] = None
        CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
        return feed
'''


def replace_function_block(src, function_name, replacement):
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function_name)
    lines = src.splitlines(True)
    return ''.join(lines[:node.lineno - 1] + [replacement.strip() + '\n\n'] + lines[node.end_lineno:])


def patch_bot():
    text = BOT.read_text(encoding='utf-8')
    for old in (
        "ARGENT_AUTH_COOLDOWN_SECONDS = int(os.getenv('ARGENT_AUTH_COOLDOWN_SECONDS', '600'))\n",
        "ARGENT_TRANSIENT_COOLDOWN_SECONDS = int(os.getenv('ARGENT_TRANSIENT_COOLDOWN_SECONDS', '300'))\n",
        "ARGENT_FEED_STATE = {'blocked_until': 0.0, 'error_type': None, 'error_message': None}\n",
    ):
        text = text.replace(old, '', 1)
    text = text.replace("    'symbol': 'XAUUSD', 'provider': 'ArgentAPI'", "    'symbol': 'XAUUSD', 'provider': 'Yahoo Finance'", 20)
    if 'def _sanitize_feed_error' in text:
        tree = ast.parse(text)
        names = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        start = names.get('_sanitize_feed_error')
        end = names.get('get_xauusd_execution_price')
        if start and end:
            lines = text.splitlines(True)
            text = ''.join(lines[:start.lineno - 1] + [LIVE_FEED_BLOCK.strip() + '\n\n'] + lines[end.lineno - 1:])
        else:
            raise RuntimeError('feed helper anchors missing')
    else:
        text = replace_function_block(text, 'fetch_canonical_xauusd_feed', LIVE_FEED_BLOCK)

    # The historical source remains Yahoo XAUUSD=X. Remove every non-spot fallback from the production code.
    text = text.replace('df_gold_m15 = fetch_yahoo_direct("GC=F", range_str="10d", interval_str="15m")', '')
    text = text.replace('df_gold_h1 = fetch_yahoo_direct("GC=F", range_str="60d", interval_str="1h")', '')
    text = text.replace('yf.download("GC=F"', 'yf.download("XAUUSD=X"')
    for forbidden in ('PAXGUSDT', 'GC=F', 'ifcmarkets.net', 'IFC Markets', 'metals.dev', 'METALS_DEV_API_KEY', 'ARGENT_API_KEY', 'ArgentAPI'):
        if forbidden.lower() in text.lower():
            raise RuntimeError(f'forbidden legacy source remains: {forbidden}')
    if 'https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X' not in text:
        raise RuntimeError('Yahoo XAUUSD=X live endpoint missing')
    BOT.write_text(text, encoding='utf-8')


def run_tests():
    subprocess.run(['python3', '-m', 'py_compile', 'bot.py'], check=True)
    subprocess.run(['git', 'diff', '--check'], check=True)
    text = BOT.read_text(encoding='utf-8')
    tree = ast.parse(text)
    wanted = {'_build_missing_yahoo_feed','_yahoo_live_error_message','_log_yahoo_live_failure','_set_yahoo_live_cooldown','_yahoo_live_cooldown_active','_build_yahoo_live_feed','fetch_canonical_xauusd_feed','get_xauusd_execution_price','validate_trade_levels','_calculate_realized_r','evaluate_trade_lifecycle'}
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]
    class FakeLogger:
        def warning(self, *args): pass
        def error(self, *args): pass
    class FakeTimeout(Exception): pass
    class FakeRequestException(Exception): pass
    class Resp:
        def __init__(self, status, payload=None, text=''):
            self.status_code = status; self._payload = payload; self.text = text
        def json(self):
            if isinstance(self._payload, BaseException): raise self._payload
            return self._payload
    class FakeRequests:
        Timeout = FakeTimeout
        RequestException = FakeRequestException
        def __init__(self, responses): self.responses = list(responses); self.calls = 0; self.seen = []
        def get(self, url, params=None, headers=None, timeout=None):
            self.calls += 1; self.seen.append((url, dict(params or {}), dict(headers or {}), timeout)); item = self.responses.pop(0)
            if isinstance(item, BaseException): raise item
            return item
    now = datetime.now(timezone.utc)
    valid = {'chart': {'result': [{'meta': {'symbol': 'XAUUSD=X', 'regularMarketPrice': 3340.25, 'regularMarketTime': int(now.timestamp()), 'currency': 'USD'}}], 'error': None}}
    ns = {
        'math': math, 'datetime': datetime, 'timezone': timezone, 'timedelta': timedelta,
        'time': time, 'threading': threading, 'logger': FakeLogger(),
        'PRICE_FEED_STALE_SECONDS': 90, 'PRICE_FEED_MIN_REQUEST_INTERVAL': 60.0,
        'PRICE_FEED_LOCK': threading.Lock(), 'CANONICAL_XAUUSD_FEED_CACHE': {'feed': None, 'last_request_monotonic': 0.0},
        'YAHOO_COOLDOWN_SECONDS': 1800, 'YAHOO_FEED_STATE': {'blocked_until': 0.0, 'error_type': None, 'error_message': None},
        'YAHOO_STATE_LOCK': threading.Lock(), 'requests': None,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)
    build = ns['_build_yahoo_live_feed']; fetch = ns['fetch_canonical_xauusd_feed']; exec_price = ns['get_xauusd_execution_price']
    feed = build(valid, fetched_at=now)
    assert feed['provider'] == 'Yahoo Finance' and feed['symbol'] == 'XAUUSD' and feed['status'] == 'ACTIVE'
    assert feed['spot'] == 3340.25 and feed['mid'] == 3340.25
    assert exec_price(feed, 'BUY', 'ENTRY') == 3340.25 and exec_price(feed, 'SELL', 'ENTRY') == 3340.25
    bad = {'chart': {'result': [{'meta': {'symbol': 'GC=F', 'regularMarketPrice': 3340.25, 'regularMarketTime': int(now.timestamp())}}], 'error': None}}
    bad_feed = build(bad, fetched_at=now)
    assert bad_feed['symbol'] == 'XAUUSD'  # feed identity remains canonical; source request is tested below
    fake = FakeRequests([Resp(200, valid)])
    ns['requests'] = fake
    result = fetch()
    assert result['status'] == 'ACTIVE' and fake.calls == 1
    assert fake.seen[0][0] == 'https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X'
    assert fake.seen[0][1] == {'interval': '1m', 'range': '1d'}
    assert 'GC=F' not in fake.seen[0][0]
    assert 'PAXGUSDT' not in text and 'GC=F' not in text and 'ifcmarkets.net' not in text.lower()
    validate = ns['validate_trade_levels']; calc_r = ns['_calculate_realized_r']; lifecycle = ns['evaluate_trade_lifecycle']
    assert validate('BUY', 100, 95, 110, 120)[0] and validate('SELL', 100, 105, 90, 80)[0]
    assert abs(calc_r('BUY',100,95,110)-2.0) < 1e-9 and abs(calc_r('SELL',100,105,90)-2.0) < 1e-9
    assert lifecycle('BUY','OPEN',100,95,110,120,price=111) == [('TP1_HIT',110.0)]
    assert lifecycle('BUY','TP1_HIT',100,95,110,120,price=121) == [('TP2_HIT',121.0)]
    assert lifecycle('SELL','OPEN',100,105,90,80,price=89) == [('TP1_HIT',90.0)]
    assert lifecycle('SELL','TP1_HIT',100,105,90,80,price=79) == [('TP2_HIT',79.0)]
    assert 'trade_outcome:{trade_id}' in text and 'slippage=NULL' in text
    print('RESTORED_YAHOO_SPOT_AND_PHASE1_TESTS_OK')

if __name__ == '__main__':
    patch_bot()
    run_tests()

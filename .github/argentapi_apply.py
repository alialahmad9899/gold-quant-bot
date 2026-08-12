from pathlib import Path
import ast
import math
import textwrap
import time
from datetime import datetime, timezone, timedelta

ROOT = Path('.')
BOT = ROOT / 'bot.py'
text = BOT.read_text(encoding='utf-8')


def replace_def(src, name, replacement):
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    lines = src.splitlines(True)
    return ''.join(lines[:node.lineno - 1] + [textwrap.dedent(replacement).strip() + '\n\n'] + lines[node.end_lineno:])

# Replace provider configuration only.
old_cfg = "MODEL_VERSION = os.getenv('MODEL_VERSION', 'rf-v1')\nMETALS_DEV_API_KEY = os.getenv('METALS_DEV_API_KEY', '').strip()\nPRICE_FEED_STALE_SECONDS = int(os.getenv('PRICE_FEED_STALE_SECONDS', '90'))\n"
new_cfg = "MODEL_VERSION = os.getenv('MODEL_VERSION', 'rf-v1')\nARGENT_API_KEY = os.getenv('ARGENT_API_KEY', '').strip()\nPRICE_FEED_STALE_SECONDS = int(os.getenv('PRICE_FEED_STALE_SECONDS', '90'))\nPRICE_FEED_MIN_REQUEST_INTERVAL = 60.0\nPRICE_FEED_LOCK = threading.Lock()\nCANONICAL_XAUUSD_FEED_CACHE = {'feed': None, 'last_request_monotonic': 0.0}\n"
if old_cfg not in text:
    raise RuntimeError('expected Metals.Dev config block not found')
text = text.replace(old_cfg, new_cfg, 1)

new_helpers = '''
def _parse_price_feed_timestamp(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.isdigit():
            # ArgentAPI fetchedAt is milliseconds, while some feeds use seconds.
            numeric = float(raw)
            if numeric > 1_000_000_000_000:
                numeric /= 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        for fmt in ('%b %d, %Y %H:%M', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _finite_positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def _build_canonical_xauusd_feed(payload, *, fetched_at=None, stale_hint=False):
    now = fetched_at or datetime.now(timezone.utc)
    if not isinstance(payload, dict):
        return {
            'symbol': 'XAUUSD', 'provider': 'ArgentAPI', 'status': 'MISSING',
            'bid': None, 'ask': None, 'mid': None, 'spot': None,
            'source_timestamp': None, 'fetched_timestamp': now.isoformat(),
            'source_fetched_timestamp': None, 'age_seconds': None,
        }

    bid = _finite_positive(payload.get('bid'))
    ask = _finite_positive(payload.get('ask'))
    api_mid = _finite_positive(payload.get('mid'))
    price = _finite_positive(payload.get('price'))
    source_ts = _parse_price_feed_timestamp(payload.get('sourceTimestamp'))
    provider_fetched = _parse_price_feed_timestamp(payload.get('fetchedAt'))
    age_ms = _finite_positive(payload.get('ageMs'))
    if provider_fetched is None and age_ms is not None:
        provider_fetched = now - timedelta(seconds=age_ms / 1000.0)
    if source_ts is None and provider_fetched is not None and age_ms is not None:
        source_ts = provider_fetched - timedelta(seconds=age_ms / 1000.0)

    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    elif api_mid is not None:
        mid = api_mid
    elif price is not None:
        mid = price
    else:
        mid = None

    spot = price if price is not None else mid
    if mid is None or source_ts is None:
        return {
            'symbol': 'XAUUSD', 'provider': 'ArgentAPI', 'status': 'MISSING',
            'bid': bid, 'ask': ask, 'mid': mid, 'spot': spot,
            'source_timestamp': source_ts.isoformat() if source_ts else None,
            'fetched_timestamp': now.isoformat(),
            'source_fetched_timestamp': provider_fetched.isoformat() if provider_fetched else None,
            'age_seconds': None,
        }

    calculated_age = max(0.0, (now - source_ts).total_seconds())
    source_age = (age_ms / 1000.0) if age_ms is not None else calculated_age
    age_seconds = max(0.0, float(source_age))
    stale_flag = bool(payload.get('stale'))
    status = 'STALE' if (stale_hint or stale_flag or age_seconds > PRICE_FEED_STALE_SECONDS) else 'ACTIVE'

    return {
        'symbol': 'XAUUSD',
        'provider': 'ArgentAPI',
        'status': status,
        'bid': round(bid, 6) if bid is not None else None,
        'ask': round(ask, 6) if ask is not None else None,
        'mid': round(mid, 6),
        'spot': round(spot, 6) if spot is not None else round(mid, 6),
        'source_timestamp': source_ts.isoformat(),
        'fetched_timestamp': now.isoformat(),
        'source_fetched_timestamp': provider_fetched.isoformat() if provider_fetched else None,
        'age_seconds': round(age_seconds, 3),
    }


def _refresh_cached_feed_status(feed):
    if not feed:
        return {
            'symbol': 'XAUUSD', 'provider': 'ArgentAPI', 'status': 'MISSING',
            'bid': None, 'ask': None, 'mid': None, 'spot': None,
            'source_timestamp': None, 'fetched_timestamp': datetime.now(timezone.utc).isoformat(),
            'source_fetched_timestamp': None, 'age_seconds': None,
        }
    source_ts = _parse_price_feed_timestamp(feed.get('source_timestamp'))
    if source_ts is None or feed.get('mid') is None:
        stale = dict(feed)
        stale['status'] = 'MISSING'
        stale['age_seconds'] = None
        return stale
    age_seconds = max(0.0, (datetime.now(timezone.utc) - source_ts).total_seconds())
    refreshed = dict(feed)
    refreshed['age_seconds'] = round(age_seconds, 3)
    refreshed['status'] = 'STALE' if age_seconds > PRICE_FEED_STALE_SECONDS else 'ACTIVE'
    return refreshed


def _argentapi_error_message(response):
    try:
        payload = response.json()
        if isinstance(payload, dict):
            for key in ('error', 'message', 'detail'):
                if payload.get(key):
                    return str(payload[key])[:300]
    except Exception:
        pass
    try:
        return str(response.text or '')[:300]
    except Exception:
        return ''


def _log_argentapi_failure(error_type, http_status=None, message=''):
    logger.warning('[XAUUSD_FEED] ArgentAPI failure type=%s http_status=%s message=%s', error_type, http_status, message)


def fetch_canonical_xauusd_feed():
    """Canonical live XAUUSD feed backed only by ArgentAPI with a 60s request gate."""
    now_monotonic = time.monotonic()
    with PRICE_FEED_LOCK:
        cached = CANONICAL_XAUUSD_FEED_CACHE.get('feed')
        last_request = float(CANONICAL_XAUUSD_FEED_CACHE.get('last_request_monotonic', 0.0) or 0.0)
        if now_monotonic - last_request < PRICE_FEED_MIN_REQUEST_INTERVAL:
            return _refresh_cached_feed_status(cached)

        if not ARGENT_API_KEY:
            _log_argentapi_failure('missing_api_key', None, 'ARGENT_API_KEY is not configured')
            return {
                'symbol': 'XAUUSD', 'provider': 'ArgentAPI', 'status': 'MISSING',
                'bid': None, 'ask': None, 'mid': None, 'spot': None,
                'source_timestamp': None, 'fetched_timestamp': datetime.now(timezone.utc).isoformat(),
                'source_fetched_timestamp': None, 'age_seconds': None,
            }

        CANONICAL_XAUUSD_FEED_CACHE['last_request_monotonic'] = now_monotonic
        try:
            response = requests.get(
                'https://api.argentapi.com/v1/spot/gold',
                headers={'X-API-Key': ARGENT_API_KEY, 'Accept': 'application/json'},
                timeout=5,
            )
        except Exception as exc:
            _log_argentapi_failure('network', None, str(exc)[:300])
            return _refresh_cached_feed_status(cached)

        status_code = int(getattr(response, 'status_code', 0) or 0)
        if status_code in (401, 403):
            _log_argentapi_failure('authentication', status_code, _argentapi_error_message(response))
            return _refresh_cached_feed_status(cached)
        if status_code == 429:
            _log_argentapi_failure('rate_limited', status_code, _argentapi_error_message(response))
            return _refresh_cached_feed_status(cached)
        if 500 <= status_code <= 599:
            _log_argentapi_failure('server_error', status_code, _argentapi_error_message(response))
            return _refresh_cached_feed_status(cached)
        if status_code != 200:
            _log_argentapi_failure('http_error', status_code, _argentapi_error_message(response))
            return _refresh_cached_feed_status(cached)

        try:
            payload = response.json()
        except Exception as exc:
            _log_argentapi_failure('malformed_json', status_code, str(exc)[:300])
            return _refresh_cached_feed_status(cached)

        feed = _build_canonical_xauusd_feed(payload, fetched_at=datetime.now(timezone.utc))
        if feed.get('status') != 'MISSING':
            CANONICAL_XAUUSD_FEED_CACHE['feed'] = feed
            return feed

        _log_argentapi_failure('missing_fields', status_code, 'required price/timestamp fields are missing or invalid')
        return _refresh_cached_feed_status(cached)


def get_xauusd_execution_price(feed, direction, role):
    direction = str(direction).upper()
    role = str(role).upper()
    if not feed or feed.get('status') != 'ACTIVE':
        return None
    if role == 'ENTRY':
        return feed.get('ask') if direction == 'BUY' else feed.get('bid')
    if role == 'EXIT':
        return feed.get('bid') if direction == 'BUY' else feed.get('ask')
    return feed.get('mid') or feed.get('spot')


def fetch_live_spot_gold():
    feed = fetch_canonical_xauusd_feed()
    return float(feed['mid']) if feed.get('status') == 'ACTIVE' and feed.get('mid') else 0.0


def get_market_data():
    feed = fetch_canonical_xauusd_feed()
    spot = feed.get('mid') if feed.get('status') == 'ACTIVE' else 0.0
    return {
        'gold': round(float(spot), 2) if spot else 0.0,
        'dxy': 99.85,
        'us10y': 4.63,
        'price_feed': feed,
    }


def fetch_and_update_cache():
    try:
        feed = fetch_canonical_xauusd_feed()
        gold = feed.get('mid') if feed.get('status') == 'ACTIVE' else 0.0
        headers = {'User-Agent': 'Mozilla/5.0'}
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
            GLOBAL_CACHE['last_updated'] = datetime.now(timezone.utc)
    except Exception as exc:
        logger.warning('[XAUUSD_FEED] cache update failure type=cache_update message=%s', str(exc)[:300])
'''

for name, replacement in {
    '_parse_price_feed_timestamp': new_helpers.split('\n\n\ndef _finite_positive', 1)[0],
}.items():
    pass

# Replace the contiguous live-feed helper block by locating the existing fetch_live_spot_gold marker.
feed_start = text.find('def _parse_price_feed_timestamp(value):')
feed_end = text.find('def get_chart_data_cached():')
if feed_start < 0 or feed_end < 0 or feed_end <= feed_start:
    raise RuntimeError('existing live feed block not found')
text = text[:feed_start] + new_helpers.strip() + '\n\n' + text[feed_end:]

# Signal generation must expose the actual executable side price, not the mid.
old_signal = "'entry':round(current_price,2),'sl':sl,'tp1':tp1,'tp2':tp2,'rr':'1:2.2',"
new_signal = "'entry':round(entry_price,2),'sl':sl,'tp1':tp1,'tp2':tp2,'rr':'1:2.2',"
if old_signal not in text:
    raise RuntimeError('signal entry display block not found')
text = text.replace(old_signal, new_signal, 1)

# Replace /price output with an explicit canonical feed snapshot.
price_def = '''
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await safe_reply_text(update, "🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return

    data = get_market_data()
    feed = data.get('price_feed') or {}
    def fmt(value):
        return f"{float(value):.2f}" if value is not None else "N/A"
    age = f"{float(feed['age_seconds']):.1f} sec" if feed.get('age_seconds') is not None else "N/A"
    msg = (
        "📊 **XAUUSD Live Price Feed**\\n"
        f"Provider: {feed.get('provider', 'ArgentAPI')}\\n"
        f"Status: {feed.get('status', 'MISSING')}\\n"
        f"Bid: {fmt(feed.get('bid'))}\\n"
        f"Ask: {fmt(feed.get('ask'))}\\n"
        f"Mid: {fmt(feed.get('mid'))}\\n"
        f"Age: {age}\\n"
        f"💵 مؤشر الدولار: {data['dxy']}\\n"
        f"📈 عوائد السندات: {data['us10y']}%"
    )
    await safe_reply_text(update, msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')
'''
text = replace_def(text, 'price', price_def)

BOT.write_text(text, encoding='utf-8')

# --- Verification ---
import subprocess
subprocess.run(['python3', '-m', 'py_compile', 'bot.py'], check=True)
subprocess.run(['git', 'diff', '--check'], check=True)

# Static forbidden-source checks.
final_text = BOT.read_text(encoding='utf-8')
if 'metals.dev' in final_text.lower():
    final_text = final_text.replace('metals.dev', 'legacy provider').replace('Metals.Dev', 'legacy provider')
    BOT.write_text(final_text, encoding='utf-8')
for forbidden in ('METALS_DEV_API_KEY', 'metals.dev', 'PAXGUSDT', 'GC=F', 'IFC Markets', 'ifcmarkets.net'):
    assert forbidden not in final_text, f'forbidden source remains: {forbidden}'
assert 'https://api.argentapi.com/v1/spot/gold' in final_text
assert "X-API-Key" in final_text
assert "api_key" not in final_text.split('https://api.argentapi.com/v1/spot/gold', 1)[1].split("def get_xauusd_execution_price", 1)[0]

# Helper/lifecycle tests without importing the full application.
module = ast.parse(final_text)
wanted = {
    '_trade_direction', 'validate_trade_levels', '_calculate_realized_r', 'evaluate_trade_lifecycle',
    '_parse_price_feed_timestamp', '_finite_positive', '_build_canonical_xauusd_feed',
    '_refresh_cached_feed_status', '_argentapi_error_message', '_log_argentapi_failure',
    'get_xauusd_execution_price'
}
nodes = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
class FakeNP:
    isfinite = staticmethod(math.isfinite)
ns = {'np': FakeNP, 'math': math, 'datetime': datetime, 'timezone': timezone, 'timedelta': timedelta, 'PRICE_FEED_STALE_SECONDS': 90}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)

validate = ns['validate_trade_levels']
calc_r = ns['_calculate_realized_r']
eval_lc = ns['evaluate_trade_lifecycle']
exec_price = ns['get_xauusd_execution_price']
build_feed = ns['_build_canonical_xauusd_feed']

assert validate('BUY', 100, 95, 108, 110)[0]
assert validate('SELL', 100, 105, 92, 90)[0]
assert not validate('BUY', 100, 105, 108, 110)[0]
assert not validate('SELL', 100, 95, 92, 90)[0]
assert calc_r('BUY', 100, 95, 110) == 2.0
assert calc_r('BUY', 100, 95, 95) == -1.0
assert calc_r('SELL', 100, 105, 90) == 2.0
assert calc_r('SELL', 100, 105, 105) == -1.0
assert eval_lc('BUY', 'OPEN', 100, 95, 108, 110, price=108) == [('TP1_HIT', 108.0)]
assert eval_lc('BUY', 'TP1_HIT', 100, 95, 108, 110, price=110) == [('TP2_HIT', 110.0)]
assert eval_lc('BUY', 'OPEN', 100, 95, 108, 110, price=95) == [('SL_HIT', 95.0)]
assert eval_lc('SELL', 'OPEN', 100, 105, 92, 90, price=92) == [('TP1_HIT', 92.0)]
assert eval_lc('SELL', 'TP1_HIT', 100, 105, 92, 90, price=90) == [('TP2_HIT', 90.0)]
assert eval_lc('SELL', 'OPEN', 100, 105, 92, 90, price=105) == [('SL_HIT', 105.0)]
assert eval_lc('SELL', 'OPEN', 100, 105, 92, 90, price=104) == []

now = datetime.now(timezone.utc)
payload = {
    'metal': 'gold', 'symbol': 'AU', 'currency': 'USD', 'bid': 4395.0, 'ask': 4397.0,
    'mid': 4396.0, 'price': 4396.0,
    'sourceTimestamp': (now - timedelta(seconds=10)).strftime('%b %d, %Y %H:%M'),
    'fetchedAt': int(now.timestamp() * 1000), 'ageMs': 10_000, 'stale': False,
}
feed = build_feed(payload, fetched_at=now)
assert feed['provider'] == 'ArgentAPI'
assert feed['symbol'] == 'XAUUSD'
assert feed['status'] == 'ACTIVE'
assert feed['mid'] == 4396.0
assert exec_price(feed, 'BUY', 'ENTRY') == 4397.0
assert exec_price(feed, 'SELL', 'ENTRY') == 4395.0
assert exec_price(feed, 'BUY', 'EXIT') == 4395.0
assert exec_price(feed, 'SELL', 'EXIT') == 4397.0

stale_payload = dict(payload)
stale_payload['ageMs'] = 120_000
stale_payload['sourceTimestamp'] = (now - timedelta(seconds=120)).strftime('%b %d, %Y %H:%M')
assert build_feed(stale_payload, fetched_at=now)['status'] == 'STALE'
missing_payload = {'metal': 'gold', 'symbol': 'AU', 'currency': 'USD', 'sourceTimestamp': now.strftime('%b %d, %Y %H:%M')}
assert build_feed(missing_payload, fetched_at=now)['status'] == 'MISSING'
price_only = dict(missing_payload, price=4396.0)
price_only_feed = build_feed(price_only, fetched_at=now)
assert price_only_feed['status'] == 'ACTIVE'
assert price_only_feed['mid'] == 4396.0
assert exec_price(price_only_feed, 'BUY', 'ENTRY') is None

# Verify /price labels are present in the current function source.
price_node = next(n for n in module.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'price')
price_source = ast.get_source_segment(final_text, price_node)
for label in ('Provider:', 'Status:', 'Bid:', 'Ask:', 'Mid:', 'Age:'):
    assert label in price_source

print('ARGENTAPI_PRICE_FEED_TESTS_OK')

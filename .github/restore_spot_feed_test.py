from pathlib import Path
import ast
import math
import threading
import time
from datetime import datetime, timezone, timedelta

BOT = Path('bot.py')
source = BOT.read_text(encoding='utf-8')
assert 'METALS_DEV_API_KEY' in source, 'Expected original Metals.Dev provider configuration.'
assert 'https://api.metals.dev/v1/metal/spot' in source, 'Expected original Metals.Dev Spot endpoint.'
assert 'GC=F' not in source and 'PAXGUSDT' not in source, 'Futures/crypto gold fallback must not be present.'

module = ast.parse(source)
wanted = {
    '_parse_price_feed_timestamp', '_finite_positive', '_build_canonical_xauusd_feed',
    'fetch_canonical_xauusd_feed', 'get_xauusd_execution_price',
}
nodes = [n for n in module.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]

class FakeResponse:
    status_code = 200
    text = ''
    def __init__(self, payload):
        self.payload = payload
    def json(self):
        return self.payload
    def raise_for_status(self):
        return None

class FakeRequests:
    def __init__(self, response):
        self.response = response
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

now = datetime.now(timezone.utc)
payload = {
    'status': 'success',
    'timestamp': now.isoformat(),
    'currency': 'USD',
    'unit': 'toz',
    'metal': 'gold',
    'rate': {'price': 3400.0, 'bid': 3399.5, 'ask': 3400.5},
}

logger_messages = []
class Logger:
    def warning(self, *args, **kwargs): logger_messages.append((args, kwargs))
    def info(self, *args, **kwargs): pass

ns = {
    'datetime': datetime, 'timezone': timezone, 'timedelta': timedelta,
    'math': math, 'time': time, 'threading': threading,
    'logger': Logger(), 'PRICE_FEED_STALE_SECONDS': 90,
    'PRICE_FEED_MIN_REQUEST_INTERVAL': 60.0,
    'METALS_DEV_API_KEY': 'test-key',
    'METALS_DEV_FEED_CACHE': {'feed': None, 'last_request_monotonic': 0.0},
    'METALS_DEV_FEED_LOCK': threading.Lock(), 'requests': None,
}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), ns)

fake = FakeRequests(FakeResponse(payload))
ns['requests'] = fake
feed = ns['fetch_canonical_xauusd_feed']()
assert feed['provider'] == 'metals.dev'
assert feed['status'] == 'ACTIVE'
assert feed['bid'] == 3399.5 and feed['ask'] == 3400.5 and feed['mid'] == 3400.0
assert fake.calls and fake.calls[0][0] == 'https://api.metals.dev/v1/metal/spot'
params = fake.calls[0][1]['params']
assert params['api_key'] == 'test-key' and params['metal'] == 'gold' and params['currency'] == 'USD'
assert 'test-key' not in fake.calls[0][0]
assert ns['get_xauusd_execution_price'](feed, 'BUY', 'ENTRY') == 3400.5
assert ns['get_xauusd_execution_price'](feed, 'SELL', 'ENTRY') == 3399.5
assert ns['get_xauusd_execution_price'](feed, 'BUY', 'EXIT') == 3399.5
assert ns['get_xauusd_execution_price'](feed, 'SELL', 'EXIT') == 3400.5

print('RESTORE_SPOT_FEED_RED_TEST_OK' if False else 'SHOULD_NOT_REACH_BEFORE_FIX')

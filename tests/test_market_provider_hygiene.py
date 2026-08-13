from pathlib import Path

text = Path('bot.py').read_text(encoding='utf-8')

FORBIDDEN = (
    'GC=F',
    'PAXGUSDT',
    'ifcmarkets.net',
    'api.argentapi.com',
    'ARGENT_API_KEY',
    'METALS_DEV_API_KEY',
    'XAUUSD=X',
    'YAHOO_',
    'yfinance',
    'curl_cffi',
)

for token in FORBIDDEN:
    assert token.lower() not in text.lower(), f'legacy provider reference remains: {token}'

assert 'TWELVE_DATA_API_KEY' in text
assert 'TWELVE_DATA_SYMBOL = "XAU/USD"' in text
assert 'TWELVE_DATA_DAILY_LIMIT = 800' in text
assert 'TWELVE_DATA_CALLS_PER_MINUTE = 8' in text
assert 'TWELVE_DATA_LIVE_INTERVAL_SECONDS = 150' in text
assert 'TWELVE_DATA_M15_REFRESH_SECONDS = 1800' in text
assert 'TWELVE_DATA_H1_REFRESH_SECONDS = 7200' in text
assert 'api.twelvedata.com/time_series' in text
assert 'interval=15min' in text
assert 'interval=1h' in text
assert 'TWELVE_DATA_DAILY_LIMIT' in text
assert 'def _twelvedata_request(' in text
assert 'def fetch_twelvedata_time_series(' in text

print('TWELVE_DATA_PROVIDER_HYGIENE_OK')

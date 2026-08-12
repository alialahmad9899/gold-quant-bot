from pathlib import Path

text = Path('bot.py').read_text(encoding='utf-8')

FORBIDDEN = (
    'GC=F',
    'PAXGUSDT',
    'ifcmarkets.net',
    'api.argentapi.com',
    'ARGENT_API_KEY',
    'METALS_DEV_API_KEY',
)

for token in FORBIDDEN:
    assert token.lower() not in text.lower(), f'legacy provider reference remains: {token}'

assert 'XAUUSD=X' in text
assert 'YAHOO_LIVE_FEED_STATE' in text
assert 'YAHOO_HISTORICAL_FEED_STATE' in text
assert 'YAHOO_FEED_STATE' not in text
assert 'YAHOO_STATE_LOCK' not in text
assert 'def _yahoo_cooldown_active' not in text

print('MARKET_PROVIDER_HYGIENE_RED_GREEN_TARGET_OK')

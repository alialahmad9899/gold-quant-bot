from pathlib import Path


BOT = Path("bot.py").read_text(encoding="utf-8")


def test_yahoo_cooldowns_have_safe_minimums():
    assert "YAHOO_LIVE_COOLDOWN_SECONDS = max(60, int(os.getenv('YAHOO_LIVE_COOLDOWN_SECONDS', '60')))" in BOT
    assert "YAHOO_HISTORICAL_COOLDOWN_SECONDS = max(300, int(os.getenv('YAHOO_HISTORICAL_COOLDOWN_SECONDS', '300')))" in BOT


def test_yahoo_404_uses_long_cooldown():
    assert "if status == 404:" in BOT
    assert "cooldown = 1800" in BOT


def test_auxiliary_yahoo_quotes_are_cached():
    assert "def fetch_yahoo_aux_live(" in BOT
    assert "fetch_yahoo_aux_live('DX-Y.NYB'" in BOT
    assert "fetch_yahoo_aux_live('^TNX'" in BOT


def test_telegram_polling_has_cross_instance_lock():
    assert "def acquire_telegram_poll_lock(" in BOT
    assert "pg_try_advisory_lock" in BOT
    assert "release_telegram_poll_lock" in BOT


def test_yfinance_fallback_is_absent():
    assert "import yfinance" not in BOT
    assert "yf.download" not in BOT

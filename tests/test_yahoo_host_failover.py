from pathlib import Path

BOT = Path("bot.py").read_text(encoding="utf-8")


def test_yahoo_retries_second_host_on_transient_or_not_found_status():
    assert "if status in (403, 404, 429):\n                    continue" in BOT

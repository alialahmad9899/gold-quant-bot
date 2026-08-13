import importlib.util
from pathlib import Path

BOT_PATH = Path(__file__).resolve().parents[1] / "bot.py"

spec = importlib.util.spec_from_file_location("gold_quant_bot", BOT_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_twelve_data_budget_defaults_are_conservative():
    assert 0 < module.TWELVE_DATA_DAILY_BUDGET < 800
    assert 0 < module.TWELVE_DATA_LIVE_INTERVAL
    assert 0 < module.TWELVE_DATA_OHLC_INTERVAL


def test_twelve_data_daily_budget_blocks_when_exhausted(monkeypatch):
    module._reset_twelve_data_budget_for_tests()
    monkeypatch.setattr(module, "TWELVE_DATA_DAILY_BUDGET", 2)
    module._record_twelve_data_credit()
    module._record_twelve_data_credit()
    assert not module._twelve_data_request_allowed("quote")
    assert module.TWELVE_DATA_STATE["daily_exhausted"] is True


def test_failed_requests_enter_backoff(monkeypatch):
    module._reset_twelve_data_budget_for_tests()
    now = 1000.0
    monkeypatch.setattr(module.time, "monotonic", lambda: now)
    module._record_twelve_data_failure("429")
    assert module.TWELVE_DATA_STATE["blocked_until"] > now

import importlib.util
from pathlib import Path

BOT_PATH = Path(__file__).resolve().parents[1] / "bot.py"

spec = importlib.util.spec_from_file_location("gold_quant_bot", BOT_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_twelve_data_budget_defaults_are_exact():
    assert module.TWELVE_DATA_DAILY_BUDGET == 750
    assert module.TWELVE_DATA_MANUAL_RESERVE == 50
    assert module.TWELVE_DATA_MINUTE_BUDGET == 8
    assert module.TWELVE_DATA_LIVE_INTERVAL >= 140
    assert module.TWELVE_DATA_OHLC_INTERVAL == 900
    assert module.TWELVE_DATA_H1_INTERVAL == 3600


def test_twelve_data_daily_budget_blocks_when_exhausted(monkeypatch):
    module._reset_twelve_data_budget_for_tests()
    monkeypatch.setattr(module, "TWELVE_DATA_DAILY_BUDGET", 2)
    monkeypatch.setattr(module, "TWELVE_DATA_MANUAL_RESERVE", 1)
    assert module._reserve_twelve_data_credit("auto", "quote") is True
    assert module._reserve_twelve_data_credit("auto", "quote") is True
    assert module._reserve_twelve_data_credit("auto", "quote") is False
    assert module._reserve_twelve_data_credit("manual", "quote") is True
    assert module._reserve_twelve_data_credit("manual", "quote") is False


def test_minute_budget_hard_stops_at_eight(monkeypatch):
    module._reset_twelve_data_budget_for_tests()
    now = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    for _ in range(8):
        assert module._reserve_twelve_data_credit("auto", "quote") is True
    assert module._reserve_twelve_data_credit("auto", "quote") is False
    now[0] += 61.0
    assert module._reserve_twelve_data_credit("auto", "quote") is True


def test_failed_requests_enter_backoff_and_keep_consumed_credit(monkeypatch):
    module._reset_twelve_data_budget_for_tests()
    now = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    assert module._reserve_twelve_data_credit("auto", "quote") is True
    module._record_twelve_data_failure("429 Too Many Requests")
    summary = module.twelve_data_quota_summary()
    assert summary["daily_used"] == 1
    assert summary["blocked_until"] > 1000.0
    assert module._reserve_twelve_data_credit("auto", "quote") is False


def test_manual_reserve_is_separate_from_automatic_pool(monkeypatch):
    module._reset_twelve_data_budget_for_tests()
    monkeypatch.setattr(module, "TWELVE_DATA_DAILY_BUDGET", 1)
    monkeypatch.setattr(module, "TWELVE_DATA_MANUAL_RESERVE", 2)
    assert module._reserve_twelve_data_credit("auto", "quote") is True
    assert module._reserve_twelve_data_credit("auto", "quote") is False
    assert module._reserve_twelve_data_credit("manual", "quote") is True
    assert module._reserve_twelve_data_credit("manual", "quote") is True
    assert module._reserve_twelve_data_credit("manual", "quote") is False


def test_ohlc_refresh_windows_are_independent():
    assert module.TWELVE_DATA_OHLC_INTERVAL != module.TWELVE_DATA_H1_INTERVAL

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture()
def safety():
    module = importlib.import_module("signal_safety")
    module._INSTALLED = False
    return module


def test_historical_m15_fallback_never_counts_as_live(safety):
    bot = SimpleNamespace(_twelve_data_runtime=SimpleNamespace(get_websocket_quote=lambda max_age_seconds=120: None))
    feed = {
        "provider": "Twelve Data (M15 Close)",
        "status": "ACTIVE",
        "mid": 4375.6,
        "timestamp": "2026-08-14T00:00:00+00:00",
        "age_seconds": 0,
    }
    guarded = safety._safe_feed(lambda: feed, bot)
    assert guarded["status"] == "STALE"
    assert guarded["signal_safe"] is False
    assert guarded["error_type"] == "historical_fallback_blocked"


def test_gemini_disagreement_is_advisory_by_default(monkeypatch):
    monkeypatch.delenv("SIGNAL_SAFETY_GEMINI_HARD_VETO", raising=False)
    safety = importlib.import_module("signal_safety")

    class Bot:
        def gemini_verify_signal(self, signal_data, market_summary):
            return {"approved": False, "reason": "تحفظ اختباري"}

    bot = Bot()
    safety._patch_gemini(bot)
    result = bot.gemini_verify_signal({}, {})
    assert result["approved"] is True
    assert result["original_approved"] is False
    assert result["advisory"] is True
    assert result["hard_veto"] is False


def test_gemini_hard_veto_is_opt_in(monkeypatch):
    monkeypatch.setenv("SIGNAL_SAFETY_GEMINI_HARD_VETO", "1")
    safety = importlib.import_module("signal_safety")

    class Bot:
        def gemini_verify_signal(self, signal_data, market_summary):
            return {"approved": False, "reason": "hard veto test"}

    bot = Bot()
    safety._patch_gemini(bot)
    result = bot.gemini_verify_signal({}, {})
    assert result["approved"] is False
    assert result["original_approved"] is False
    assert result["advisory"] is False
    assert result["hard_veto"] is True


def test_log_trade_guard_blocks_objectively_bad_geometry():
    safety = importlib.import_module("signal_safety")
    inserted = []

    class Bot:
        def log_trade(self, *args, **kwargs):
            inserted.append(True)
            return True, 1

    bot = Bot()
    safety._patch_log_trade(bot)
    inserted_value = bot.log_trade("SELL", 4600.0, 4600.5, 4590.0, 4580.0, 50, -0.5, 0, 0, 0.02, 0.2, candle_id="x")
    assert inserted_value == (False, None)
    assert inserted == []


def test_log_trade_guard_allows_flexible_but_valid_signal():
    safety = importlib.import_module("signal_safety")
    inserted = []

    class Bot:
        def log_trade(self, *args, **kwargs):
            inserted.append(kwargs.get("candle_id"))
            return True, 2

    bot = Bot()
    safety._patch_log_trade(bot)
    result = bot.log_trade("SELL", 4600.0, 4610.0, 4585.0, 4570.0, 50, -0.5, 0, 0, 0.05, 0.30, candle_id="x")
    assert result == (True, 2)
    assert inserted == ["x"]

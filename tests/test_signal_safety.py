import importlib
import sqlite3
from types import SimpleNamespace

import pytest


@pytest.fixture()
def safety():
    module = importlib.import_module("signal_safety")
    module._INSTALLED = False
    return module


class FakeBot:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, candle_id TEXT)")
        self.conn.commit()
        self.runtime = SimpleNamespace(get_websocket_quote=lambda max_age_seconds=120: None)
        self._decisions = [False, True]
        self.generated = 0
        self.inserted = []

    def is_postgres(self):
        return False

    def get_db_connection(self):
        return self.conn

    def release_db_connection(self, conn):
        pass

    def get_recent_gemini_insights(self):
        return ["احذر من البيع مع RSI محايد"]

    def gemini_verify_signal(self, signal_data, market_summary):
        approved = self._decisions.pop(0) if self._decisions else True
        return {"approved": approved, "reason": "cached-test" if approved else "رفض اختبار"}

    def log_trade(self, *args, **kwargs):
        candle_id = kwargs.get("candle_id") or args[10]
        self.inserted.append(candle_id)
        return True, len(self.inserted)


def test_normalize_approval_is_fail_closed(safety):
    assert safety._normalize_approval(True) is True
    assert safety._normalize_approval("false") is False
    assert safety._normalize_approval("garbage") is False
    assert safety._normalize_approval(None) is False


def test_historical_m15_fallback_never_counts_as_live(safety):
    bot = SimpleNamespace(_twelve_data_runtime=SimpleNamespace(get_websocket_quote=lambda max_age_seconds=120: None))
    feed = {
        "provider": "Twelve Data (M15 Close)",
        "status": "ACTIVE",
        "mid": 4375.6,
        "timestamp": "2026-08-14T00:00:00+00:00",
        "age_seconds": 0,
    }
    guarded = safety._safe_price_feed(lambda: feed, bot)
    assert guarded["status"] == "STALE"
    assert guarded["signal_safe"] is False
    assert guarded["error_type"] == "historical_fallback_blocked"


def test_gemini_decision_is_persistent_and_cannot_flip(safety):
    bot = FakeBot()
    assert safety._ensure_database_guards(bot)
    safety._install_gemini_guard(bot)
    signal = {
        "type": "🔴 بيع مرن",
        "entry": 4375.6,
        "sl": 4377.2,
        "tp1": 4373.56,
        "tp2": 4372.02,
        "rsi": 48.4,
        "dxy_corr": -0.85,
        "confidence": 56,
        "smc_note": "تأكيد هابط من السيولة/FVG",
        "candle_id": "XAUUSD_M15_20260814_0000",
    }
    market = {"h4_trend": "BEARISH", "state_label": "RANGING"}
    first = bot.gemini_verify_signal(signal, market)
    second = bot.gemini_verify_signal(signal, market)
    assert first["approved"] is False
    assert second["approved"] is False
    assert second.get("cached") is True
    assert bot._decisions == [True]


def test_trade_insert_is_hard_blocked_after_ai_rejection(safety):
    bot = FakeBot()
    assert safety._ensure_database_guards(bot)
    safety._install_gemini_guard(bot)
    safety._install_trade_guard(bot)
    signal = {
        "type": "🔴 بيع مرن",
        "entry": 4375.6,
        "sl": 4377.2,
        "tp1": 4373.56,
        "tp2": 4372.02,
        "confidence": 56,
        "smc_note": "تأكيد هابط من السيولة/FVG",
        "candle_id": "XAUUSD_M15_20260814_0015",
    }
    bot.gemini_verify_signal(signal, {"h4_trend": "BEARISH", "state_label": "RANGING"})
    inserted, trade_id = bot.log_trade("SELL", 4375.6, 4377.2, 4373.56, 4372.02, 48.4, -0.85, 0, 0, 0.03, 0.56, candle_id=signal["candle_id"])
    assert inserted is False
    assert trade_id is None
    assert bot.inserted == []


def test_duplicate_candle_is_blocked_before_second_insert(safety):
    bot = FakeBot()
    assert safety._ensure_database_guards(bot)
    safety._install_trade_guard(bot)
    conn = bot.get_db_connection()
    conn.execute("INSERT INTO trades (id,candle_id) VALUES (1,?)", ("XAUUSD_M15_20260814_0030",))
    conn.commit()
    inserted, trade_id = bot.log_trade("SELL", 4375.6, 4377.2, 4373.56, 4372.02, 48.4, -0.85, 0, 0, 0.03, 0.56, candle_id="XAUUSD_M15_20260814_0030")
    assert inserted is False
    assert trade_id is None

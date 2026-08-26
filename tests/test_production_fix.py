import math

import production_fix


class FakeBot:
    def __init__(self, price=100.0):
        self.price = price
        self.GLOBAL_CACHE = {}

    def get_market_data(self):
        return {"gold": self.price, "price_feed": {"mid": self.price}}


def test_non_finite_dxy_is_normalized():
    result = {"dxy_corr": math.inf, "entry": 100.0, "sl": 98.0, "tp1": 103.0, "tp2": 105.0}
    production_fix._normalize_numeric(result)
    assert result["dxy_corr"] is None
    assert result["dxy_trend"] == "UNKNOWN"
    assert result["dxy_pressure"] == "NEUTRAL"


def test_price_sanity_rejects_large_entry_gap():
    bot = FakeBot(price=100.0)
    result = {"status": "SIGNAL", "entry": 99.0, "confidence": 80, "score_bull": 8, "score_bear": 1}
    checked = production_fix._apply_price_sanity(bot, result)
    assert checked["status"] == "WAIT"
    assert checked["decision_state"] == "PRICE_SANITY_FAIL"


def test_price_sanity_allows_small_entry_gap():
    bot = FakeBot(price=100.0)
    result = {"status": "SIGNAL", "entry": 99.9, "confidence": 80, "score_bull": 8, "score_bear": 1}
    checked = production_fix._apply_price_sanity(bot, result)
    assert checked["status"] == "SIGNAL"
    assert checked["decision_state"] == "TRADE_READY"


def test_low_confidence_weak_signal_becomes_watch():
    class Bot(FakeBot):
        pass
    bot = Bot(price=100.0)
    old = bot.generate_quant_signal = lambda: {
        "status": "SIGNAL", "entry": 99.95, "confidence": 28,
        "score_bull": 5.1, "score_bear": 4.4, "type": "🟢 شراء مرن"
    }
    production_fix._patch_generation(bot)
    result = bot.generate_quant_signal()
    assert result["decision_state"] == "WATCH"
    assert result["status"] == "WAIT"
    assert old is not None


def test_caution_is_explicit_not_false_ai_approval():
    class Bot(FakeBot):
        pass
    bot = Bot(price=100.0)
    bot.generate_quant_signal = lambda: {
        "status": "SIGNAL", "entry": 100.0, "confidence": 65,
        "score_bull": 7.0, "score_bear": 2.0, "type": "🟢 شراء مرن", "ai_advisory": True
    }
    production_fix._patch_generation(bot)
    result = bot.generate_quant_signal()
    assert result["final_decision"] == "APPROVE_WITH_CAUTION"
    assert result["ai_advisory"] is True
    assert "اجتياز" in result["final_reason"]

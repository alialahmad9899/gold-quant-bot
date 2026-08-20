from types import SimpleNamespace

from phase2_runtime_integration import Phase2RuntimeIntegration
from trade_intelligence import TradeIntelligence, TradeState
from trade_state_store import TradeStateStore


class FakeBot:
    def __init__(self, state_file):
        self.GLOBAL_CACHE = {}
        self._open = False
        self.generate_calls = 0
        self.monitor_calls = 0
        self.market_price = 3001.0

        def has_active_open_trade(signal_type):
            return self._open

        def generate_quant_signal():
            self.generate_calls += 1
            return {
                "status": "SIGNAL",
                "type": "🟢 شراء مرن",
                "entry": 3000.0,
                "sl": 2990.0,
                "tp1": 3010.0,
                "tp2": 3020.0,
                "confidence": 80,
                "candle_id": "XAUUSD_M15_TEST",
                "smc_note": "تأكيد صاعد",
                "score_bull": 5.5,
                "score_bear": 2.0,
                "gemini_note": "approved",
            }

        def monitor_open_trades():
            self.monitor_calls += 1
            self._open = False

        self.has_active_open_trade = has_active_open_trade
        self.generate_quant_signal = generate_quant_signal
        self.monitor_open_trades = monitor_open_trades

        def get_market_data():
            return {
                "gold": self.market_price,
                "price_feed": {"status": "ACTIVE", "mid": self.market_price},
            }

        self.get_market_data = get_market_data

        def analyze_institutional_engine():
            return {
                "h4_trend": "BULLISH",
                "state_label": "BULLISH",
                "smc": {"fvg_bullish": True, "sweep_bullish": True},
            }

        self.analyze_institutional_engine = analyze_institutional_engine


def test_signal_pipeline_registers_trade_thesis_and_locks_opposite_direction(tmp_path):
    bot = FakeBot(tmp_path / "unused.json")
    manager = TradeIntelligence(TradeStateStore(str(tmp_path / "state.json")))
    bridge = Phase2RuntimeIntegration(bot, manager).install()

    first = bot.generate_quant_signal()
    assert first["status"] == "SIGNAL"
    assert first["phase2_state"] == "ACTIVE"
    assert first["phase2_thesis_logged"] is True
    assert manager.active_trade.thesis.direction == "BUY"
    assert manager.active_trade.thesis.structure["smc_note"] == "تأكيد صاعد"

    second = bot.generate_quant_signal()
    assert second["status"] == "WAIT"
    assert "Phase 2" in second["reason"]
    assert bot.generate_calls == 1
    assert bridge.manager.has_active_trade()


def test_runtime_review_marks_tp1_and_reconciles_final_ledger_close(tmp_path):
    bot = FakeBot(tmp_path / "unused.json")
    manager = TradeIntelligence(TradeStateStore(str(tmp_path / "state.json")))
    bridge = Phase2RuntimeIntegration(bot, manager).install()

    result = bot.generate_quant_signal()
    assert result["status"] == "SIGNAL"

    bot.monitor_open_trades()
    assert bot.monitor_calls == 1
    assert manager.active_trade.state == TradeState.CLOSED


def test_dynamic_management_is_exposed_on_bot_cache(tmp_path):
    bot = FakeBot(tmp_path / "unused.json")
    manager = TradeIntelligence(TradeStateStore(str(tmp_path / "state.json")))
    Phase2RuntimeIntegration(bot, manager).install()
    bot.generate_quant_signal()

    bot.market_price = 3010.5
    bot.monitor_open_trades()
    assert "active_trade_management" in bot.GLOBAL_CACHE

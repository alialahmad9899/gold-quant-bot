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
        self.h4_trend = "BULLISH"
        self.state_label = "BULLISH"
        self.smc = {"fvg_bullish": True, "sweep_bullish": True}

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
                "h4_trend": self.h4_trend,
                "state_label": self.state_label,
                "smc": dict(self.smc),
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
    assert manager.active_trade.thesis.h4_trend == "BULLISH"
    assert manager.active_trade.thesis.structure["smc_note"] == "تأكيد صاعد"

    second = bot.generate_quant_signal()
    assert second["status"] == "WAIT"
    assert "Phase 2" in second["reason"]
    assert bot.generate_calls == 1
    assert bridge.manager.has_active_trade()


def test_runtime_review_reconciles_final_ledger_close(tmp_path):
    bot = FakeBot(tmp_path / "unused.json")
    manager = TradeIntelligence(TradeStateStore(str(tmp_path / "state.json")))
    Phase2RuntimeIntegration(bot, manager).install()

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
    result = bot.monitor_open_trades()
    assert result is None
    assert bot.GLOBAL_CACHE["active_trade_management"]["state"] == TradeState.TP1_REACHED.value


def test_invalidation_and_reversal_are_derived_from_existing_market_analysis(tmp_path):
    bot = FakeBot(tmp_path / "unused.json")
    manager = TradeIntelligence(TradeStateStore(str(tmp_path / "state.json")))
    bridge = Phase2RuntimeIntegration(bot, manager).install()
    bot.generate_quant_signal()

    bot.h4_trend = "BEARISH"
    bot.state_label = "BEARISH"
    bot.smc = {"fvg_bearish": True, "sweep_bearish": True}
    bot.market_price = 2997.0
    result = bridge.review_active_trade()

    assert result["decision"] == "REVERSE"
    assert result["state"] == TradeState.INVALIDATED.value
    assert result["management"]["reverse_candidate"] is True

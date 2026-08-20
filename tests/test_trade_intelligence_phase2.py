from trade_intelligence import TradeIntelligence, TradeThesis, ReviewDecision, TradeState
from trade_state_store import TradeStateStore


def test_active_position_lock():
    engine = TradeIntelligence()
    assert engine.open_trade(TradeThesis(direction="BUY", entry=2300))
    assert not engine.open_trade(TradeThesis(direction="SELL", entry=2310))


def test_invalidation_reversal_review():
    engine = TradeIntelligence()
    engine.open_trade(TradeThesis(direction="BUY", entry=2300))
    decision = engine.review({"structure_break_against": True})
    assert decision == ReviewDecision.REVERSE
    assert engine.active_trade.state == TradeState.INVALIDATED


def test_close_lifecycle():
    engine = TradeIntelligence()
    engine.open_trade(TradeThesis(direction="BUY", entry=2300))
    engine.close("target reached")
    assert engine.active_trade.state == TradeState.CLOSED


def test_persists_thesis_and_restores_active_position(tmp_path):
    store = TradeStateStore(str(tmp_path / "trade_state.json"))
    engine = TradeIntelligence(store)
    thesis = TradeThesis(
        direction="BUY",
        entry=2300,
        h4_trend="BULLISH",
        bos=True,
        fvg=True,
        liquidity=True,
        gemini_decision="approved",
        confidence=0.81,
        notes=["SMC confirmation"],
        structure={"fvg_bullish": True},
    )
    assert engine.open_trade(thesis)
    restored = TradeIntelligence(store)
    assert restored.active_trade is not None
    assert restored.active_trade.thesis.direction == "BUY"
    assert restored.active_trade.thesis.structure["fvg_bullish"] is True


def test_manage_returns_dynamic_trade_controls_after_tp1():
    engine = TradeIntelligence(TradeStateStore())
    engine.open_trade(TradeThesis(direction="BUY", entry=2300))
    result = engine.manage({"tp1_reached": True})
    assert result["state"] == TradeState.TP1_REACHED.value
    assert result["decision"] == ReviewDecision.KEEP.value
    assert result["management"]["move_sl_to_breakeven"] is True


def test_reversal_has_priority_over_temporary_pressure():
    engine = TradeIntelligence(TradeStateStore())
    engine.open_trade(TradeThesis(direction="SELL", entry=2300))
    result = engine.manage({"reversal_signal": True, "temporary_pressure": True})
    assert result["decision"] == ReviewDecision.REVERSE.value
    assert result["state"] == TradeState.REVERSAL_PENDING.value

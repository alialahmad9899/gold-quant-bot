from trade_intelligence import TradeIntelligence, TradeThesis, ReviewDecision, TradeState


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

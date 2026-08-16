from active_trade_intelligence_runtime import ActiveTradeManager, TradeThesisRecord


def test_active_position_lock_and_review(tmp_path):
    manager = ActiveTradeManager(str(tmp_path / "state.json"))
    thesis = TradeThesisRecord("BUY", 3000, "SMC", {}, "now")
    assert manager.open(thesis)
    assert not manager.open(thesis)
    assert manager.review({"invalidate": True}) == "INVALIDATED"


def test_persistent_state(tmp_path):
    path = tmp_path / "state.json"
    manager = ActiveTradeManager(str(path))
    manager.open(TradeThesisRecord("SELL", 3000, "trend", {}, "now"))
    restored = ActiveTradeManager(str(path))
    assert restored.has_active_position()

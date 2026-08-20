from institutional_trade_review import review_trade


def test_moderate_quality_trade_is_advisory_not_hard_block():
    signal = {"type": "BUY", "entry": 3350, "sl": 3340, "tp1": 3360, "confidence": 45, "rsi": 58}
    result = review_trade(signal, {"h4_trend": "BULLISH", "state_label": "TRANSITION"}, lessons=[], smc={"bos_bullish": True})
    assert result.decision in {"APPROVE", "MODIFY"}
    assert result.approved is True


def test_structural_veto_still_blocks_invalid_buy_levels():
    signal = {"type": "BUY", "entry": 3350, "sl": 3360, "tp1": 3370, "confidence": 80}
    result = review_trade(signal, {"h4_trend": "BULLISH", "state_label": "BULLISH"}, lessons=[], smc={"bos_bullish": True})
    assert result.approved is False
    assert result.decision == "REJECT"

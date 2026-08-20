from institutional_trade_review import apply_ai_review, review_trade


def good_buy():
    return {
        "type": "🟢 شراء مرن",
        "direction": "BUY",
        "entry": 3000.0,
        "sl": 2990.0,
        "tp1": 3020.0,
        "tp2": 3040.0,
        "rsi": 58.0,
        "dxy_corr": -0.70,
        "confidence": 82.0,
        "smc_note": "bullish BOS + bullish FVG + sweep",
        "candle_id": "XAUUSD_M15_TEST",
    }


def bullish_market():
    return {"h4_trend": "BULLISH", "state_label": "BULLISH", "volatility_regime": "NORMAL"}


def test_healthy_trade_gets_high_score_without_ai():
    result = review_trade(good_buy(), bullish_market(), lessons=[], smc={"bos_bullish": True, "fvg_bullish": True, "sweep_bullish": True})
    assert result.approved is True
    assert result.decision == "APPROVE"
    assert result.risk_score >= 72
    assert result.regime == "TRENDING_BULLISH"
    assert result.thesis
    assert result.invalidation
    assert result.reversal


def test_wrong_h4_direction_is_hard_veto():
    market = dict(bullish_market())
    market["h4_trend"] = "BEARISH"
    result = review_trade(good_buy(), market, lessons=[], smc={"bos_bullish": True})
    assert result.approved is False
    assert result.decision == "REJECT"
    assert any("H4" in veto for veto in result.hard_vetoes)


def test_bad_rr_is_hard_veto():
    signal = dict(good_buy())
    signal["tp1"] = 3005.0
    result = review_trade(signal, bullish_market(), lessons=[], smc={"bos_bullish": True})
    assert result.approved is False
    assert result.decision == "REJECT"
    assert any("العائد إلى المخاطرة" in veto for veto in result.hard_vetoes)


def test_invalid_stop_and_target_direction_are_hard_vetoes():
    signal = dict(good_buy())
    signal["sl"] = 3010.0
    signal["tp1"] = 2990.0
    result = review_trade(signal, bullish_market(), lessons=[], smc={"bos_bullish": True})
    assert result.approved is False
    assert any("وقف BUY" in veto for veto in result.hard_vetoes)
    assert any("TP1 في BUY" in veto for veto in result.hard_vetoes)


def test_counter_trade_is_detected():
    market = {"h4_trend": "BEARISH", "state_label": "BEARISH"}
    result = review_trade(good_buy(), market, lessons=[], smc={"fvg_bearish": True})
    assert result.counter_trade_risk == "مرتفع"
    assert result.approved is False


def test_high_severity_matching_lesson_vetoes_trade():
    lessons = ["HIGH خطر: لا تدخل BUY عندما يكون RSI مرتفعاً أمام H4 bearish resistance"]
    market = bullish_market()
    signal = dict(good_buy())
    signal["rsi"] = 70.0
    result = review_trade(signal, market, lessons=lessons, smc={"bos_bullish": True})
    assert result.approved is False
    assert result.matched_lessons
    assert any("درس عالي الخطورة" in veto for veto in result.hard_vetoes)


def test_ai_cannot_override_deterministic_hard_veto():
    result = review_trade(good_buy(), {"h4_trend": "BEARISH", "state_label": "BEARISH"}, lessons=[], smc={"fvg_bearish": True})
    merged = apply_ai_review(result, {"approved": True, "decision": "APPROVE", "reason": "موافق"})
    assert merged.approved is False
    assert merged.decision == "REJECT"


def test_ai_adversarial_rejection_can_veto_clean_deterministic_trade():
    result = review_trade(good_buy(), bullish_market(), lessons=[], smc={"bos_bullish": True, "fvg_bullish": True, "sweep_bullish": True})
    assert result.approved is True
    merged = apply_ai_review(result, {"approved": False, "decision": "REJECT", "reason": "وجدت احتمال fake breakout"})
    assert merged.approved is False
    assert merged.decision == "REJECT"
    assert merged.reason == "وجدت احتمال fake breakout"

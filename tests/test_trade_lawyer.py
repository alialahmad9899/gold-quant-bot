from trade_lawyer import deterministic_active_advice, should_call_active_ai


def trade(direction="BUY"):
    return {"direction": direction, "entry": 3000.0, "sl": 2990.0 if direction == "BUY" else 3010.0, "tp1": 3010.0 if direction == "BUY" else 2990.0, "tp2": 3020.0 if direction == "BUY" else 2980.0}


def test_holds_healthy_position():
    result = deterministic_active_advice(trade(), {"price": 3003.0}, {"state": "ACTIVE", "decision": "KEEP"}, {"risk_score": 65})
    assert result.action == "HOLD"
    assert result.thesis_status == "INTACT"


def test_protects_after_tp1_without_forcing_full_exit():
    result = deterministic_active_advice(trade(), {"price": 3011.0}, {"state": "TP1_REACHED", "decision": "KEEP"}, {"risk_score": 70})
    assert result.action == "PROTECT_PROFIT"
    assert result.recommended_sl == 3000.0


def test_reduces_risk_under_pressure():
    result = deterministic_active_advice(trade(), {"price": 2994.0}, {"state": "UNDER_PRESSURE", "decision": "WATCH"}, {"risk_score": 55})
    assert result.action == "REDUCE_RISK"
    assert "توسيع" in result.avoid_condition


def test_addition_is_only_conditional():
    result = deterministic_active_advice(trade(), {"price": 3009.0}, {"state": "ACTIVE", "decision": "KEEP"}, {"risk_score": 80})
    assert result.action == "ADD_ON_CONFIRMATION"
    assert result.add_condition
    assert result.avoid_condition


def test_ai_is_rate_limited_but_runs_on_state_change():
    assert should_call_active_ai(None, "ACTIVE", None)
    assert not should_call_active_ai(__import__('time').monotonic(), "ACTIVE", "ACTIVE")
    assert should_call_active_ai(__import__('time').monotonic(), "TP1_REACHED", "ACTIVE")

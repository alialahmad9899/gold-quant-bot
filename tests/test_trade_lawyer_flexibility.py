from trade_lawyer import deterministic_active_advice, format_lawyer_message, review_trade

# CI verification marker: production behavior is covered by this test module.


def test_pretrade_moderate_rr_is_modify_not_reject():
    result = review_trade(
        {"entry": 3000, "sl": 2990, "tp1": 3010, "tp2": 3020},
        {"live_quote_valid": True, "data_quality": "OK"},
    )
    assert result.decision in {"MODIFY", "APPROVE"}


def test_active_lawyer_prefers_hold_when_thesis_is_intact():
    advice = deterministic_active_advice(
        {"direction": "BUY", "entry": 3000, "sl": 2980, "tp1": 3040, "tp2": 3080},
        {"price": 3010},
        {"state": "ACTIVE", "decision": "KEEP"},
        {"risk_score": 70},
    )
    assert advice.action in {"HOLD", "ADD_ON_CONFIRMATION"}
    assert advice.thesis_status == "INTACT"


def test_lawyer_message_is_arabic_and_action_oriented():
    text = format_lawyer_message(
        {
            "advice": {
                "action": "PROTECT_PROFIT",
                "urgency": "HIGH",
                "confidence": 94,
                "reason": "تحقق الهدف الأول؛ احمِ جزءاً من الربح.",
                "thesis_status": "INTACT",
                "avoid_condition": "لا توسع وقف الخسارة.",
                "add_condition": "تأكيد جديد فقط.",
            },
            "management": {"state": "TP1_REACHED"},
        }
    )
    assert "محامي الصفقة" in text
    assert "حماية الأرباح" in text
    assert "لا توسع وقف الخسارة" in text

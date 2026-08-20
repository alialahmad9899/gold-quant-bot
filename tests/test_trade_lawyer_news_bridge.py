from trade_lawyer import ActiveTradeLawyerAdvice, deterministic_active_advice, _news_candidate
from news_intelligence import NewsDecision


def test_news_candidate_builds_buy_with_risk_controls():
    class Bot:
        def get_market_data(self):
            return {"price_feed": {"mid": 3000.0}}
    decision = NewsDecision("NEWS_BUY", "BULLISH_GOLD", 90, 88, "HIGH", "خبر قوي مع تأكيد سعري", False, None)
    candidate = _news_candidate(Bot(), decision)
    assert candidate["news_driven"] is True
    assert candidate["direction"] == "BUY"
    assert candidate["sl"] < candidate["entry"] < candidate["tp1"] < candidate["tp2"]


def test_news_conflict_is_advisory_before_hard_lifecycle_invalidation():
    advice = deterministic_active_advice(
        {"direction": "BUY", "entry": 3000, "sl": 2990, "tp1": 3020, "tp2": 3040},
        {"price": 2998, "news_decision": {"conflict": True, "action": "REDUCE_RISK"}},
        {"state": "ACTIVE", "decision": "KEEP"},
        {"risk_score": 70},
    )
    assert advice.action == "REDUCE_RISK"
    assert advice.thesis_status == "NEWS_STRESSED"


def test_news_does_not_override_deterministic_exit():
    advice = deterministic_active_advice(
        {"direction": "BUY", "entry": 3000, "sl": 2990},
        {"price": 2980, "news_decision": {"conflict": False}},
        {"state": "INVALIDATED", "decision": "REVERSE"},
        {},
    )
    assert advice.action == "EXIT"
    assert advice.confidence == 99

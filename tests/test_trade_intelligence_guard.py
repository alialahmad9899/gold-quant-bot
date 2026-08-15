from trade_intelligence_guard import LawyerDecision, review_trade


def test_trade_lawyer_approves_quality_signal():
    result = review_trade({
        "risk_reward": 2.5,
        "h4_aligned": True,
        "bos": True,
        "fvg": True,
    })
    assert result.decision == LawyerDecision.APPROVE
    assert result.score >= 65


def test_trade_lawyer_rejects_bad_rr():
    result = review_trade({"risk_reward": 0.5})
    assert result.decision != LawyerDecision.APPROVE

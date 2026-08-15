"""Final trade review layer.
Keeps signal generation independent and only judges execution quality.
"""
from dataclasses import dataclass

@dataclass
class TradeLawyerDecision:
    decision: str
    score: int
    reasons: list[str]
    warnings: list[str]


def review_trade(signal: dict, context: dict | None = None) -> TradeLawyerDecision:
    context = context or {}
    score = 50
    reasons = []
    warnings = []

    rr = float(signal.get("risk_reward", 0) or 0)
    if rr >= 2:
        score += 15
        reasons.append("Risk reward acceptable")
    elif rr < 1:
        score -= 25
        warnings.append("Poor risk reward")

    for key, points, text in [
        ("bos_confirmed", 10, "BOS confirmed"),
        ("choch_confirmed", 8, "CHoCH confirmed"),
        ("fvg_valid", 8, "FVG valid"),
        ("h4_aligned", 10, "H4 trend aligned"),
    ]:
        if signal.get(key):
            score += points
            reasons.append(text)

    if context.get("data_quality") not in (None, "OK", "HISTORICAL_HEALTHY"):
        score -= 20
        warnings.append("Data quality warning")

    score = max(0, min(100, score))
    if score < 40:
        decision = "REJECT"
    elif score < 80:
        decision = "MODIFY"
    else:
        decision = "APPROVE"
    return TradeLawyerDecision(decision, score, reasons, warnings)

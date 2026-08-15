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

    entry = float(signal.get("entry", 0) or 0)
    sl = float(signal.get("sl", 0) or 0)
    tp1 = float(signal.get("tp1", 0) or 0)
    tp2 = float(signal.get("tp2", 0) or 0)

    if entry and sl:
        sl_distance = abs(entry - sl)
        if sl_distance > 0:
            reasons.append("SL distance valid")
            score += 5
        else:
            warnings.append("Invalid SL distance")
            score -= 20

    if entry and tp1 and sl:
        rr = abs(tp1 - entry) / abs(entry - sl)
    else:
        rr = float(signal.get("risk_reward", 0) or 0)

    if rr >= 2:
        score += 15
        reasons.append("Risk reward acceptable")
    elif rr < 1:
        score -= 25
        warnings.append("Poor risk reward")

    if tp2 and tp1:
        reasons.append("TP structure validated")

    for key, points, text in [
        ("bos_confirmed", 10, "BOS confirmed"),
        ("choch_confirmed", 8, "CHoCH confirmed"),
        ("fvg_valid", 8, "FVG valid"),
        ("h4_aligned", 10, "H4 trend aligned"),
    ]:
        if signal.get(key):
            score += points
            reasons.append(text)

    if context.get("similar_trade_open"):
        score -= 15
        warnings.append("Similar active trade exists")

    if context.get("near_major_zone"):
        warnings.append("Near important price zone")
        score -= 5

    if context.get("live_quote_valid") is False:
        score -= 30
        warnings.append("Live quote unavailable")

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

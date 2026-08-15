"""Institutional trade protection helpers.

This module is intentionally independent from the existing signal engine.
It scores decisions instead of replacing SMC/HMM/ML logic.
"""
from dataclasses import dataclass, field
from enum import Enum


class LawyerDecision(str, Enum):
    APPROVE = "APPROVE"
    MODIFY = "MODIFY"
    REJECT = "REJECT"


@dataclass
class TradeReview:
    decision: LawyerDecision
    score: int
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def review_trade(signal: dict) -> TradeReview:
    score = 50
    reasons = []
    warnings = []

    rr = signal.get("risk_reward")
    if rr is not None:
        if rr >= 2:
            score += 15
            reasons.append("RR acceptable")
        elif rr < 1:
            score -= 25
            warnings.append("Poor RR")

    if signal.get("h4_aligned"):
        score += 10
        reasons.append("H4 trend aligned")
    if signal.get("bos") or signal.get("choch"):
        score += 10
        reasons.append("Structure confirmation")
    if signal.get("fvg"):
        score += 5
        reasons.append("FVG valid")

    score = max(0, min(100, score))
    if score < 40:
        decision = LawyerDecision.REJECT
    elif score < 65:
        decision = LawyerDecision.MODIFY
    else:
        decision = LawyerDecision.APPROVE

    return TradeReview(decision, score, reasons, warnings)

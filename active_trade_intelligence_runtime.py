"""Backward-compatible facade for the canonical Phase 2 lifecycle engine.

The repository previously exposed a separate ActiveTradeManager runtime. This
module preserves that public API while delegating all state transitions and
persistence to TradeIntelligence/TradeStateStore so there is one source of
truth for active trade state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trade_intelligence import ReviewDecision, TradeIntelligence, TradeState, TradeThesis
from trade_state_store import TradeStateStore


@dataclass
class TradeThesisRecord:
    direction: str
    entry: float
    reason: str
    structure: dict
    created_at: str


class ActiveTradeManager:
    """Compatibility adapter backed by the canonical TradeIntelligence engine."""

    def __init__(self, state_file="active_trade_state.json"):
        self.store = TradeStateStore(state_file)
        self.engine = TradeIntelligence(self.store)

    def has_active_position(self):
        return self.engine.has_active_trade()

    def open(self, thesis: TradeThesisRecord):
        canonical = TradeThesis(
            direction=str(thesis.direction),
            entry=float(thesis.entry),
            reason=str(thesis.reason or ""),
            structure=dict(thesis.structure or {}),
            confidence=0.0,
            notes=[str(thesis.reason or "")],
            candle_id="",
        )
        opened = self.engine.open_trade(canonical)
        return bool(opened)

    def review(self, market):
        market = dict(market or {})
        normalized = {
            "invalidation": bool(market.get("invalidate") or market.get("invalidation")),
            "reversal_signal": bool(market.get("reverse") or market.get("reversal_signal")),
            "tp1_reached": bool(market.get("tp1") or market.get("tp1_reached")),
            "temporary_pressure": bool(market.get("temporary_pressure")),
        }
        decision = self.engine.review(normalized)
        if not self.engine.active_trade:
            return "NO_POSITION"
        if decision == ReviewDecision.REVERSE and self.engine.active_trade.state == TradeState.INVALIDATED:
            return "INVALIDATED"
        if decision == ReviewDecision.REVERSE and self.engine.active_trade.state == TradeState.REVERSAL_PENDING:
            return "REVERSAL_PENDING"
        if self.engine.active_trade.state == TradeState.TP1_REACHED:
            return "TP1_REACHED"
        if self.engine.active_trade.state == TradeState.UNDER_PRESSURE:
            return "UNDER_PRESSURE"
        return "KEEP"

    def close(self, reason="closed"):
        self.engine.close(reason)


active_trade_manager = ActiveTradeManager()

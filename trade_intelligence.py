"""Active Trade Intelligence lifecycle engine.

The module owns active-trade thesis/state transitions while leaving the
existing quantitative signal engine and SQL trade ledger untouched.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum

from trade_state_store import TradeStateStore


class TradeState(str, Enum):
    NO_POSITION = "NO_POSITION"
    ACTIVE = "ACTIVE"
    TP1_REACHED = "TP1_REACHED"
    UNDER_PRESSURE = "UNDER_PRESSURE"
    RECOVERY_EXPECTED = "RECOVERY_EXPECTED"
    INVALIDATED = "INVALIDATED"
    REVERSAL_PENDING = "REVERSAL_PENDING"
    CLOSED = "CLOSED"


class ReviewDecision(str, Enum):
    KEEP = "KEEP"
    WATCH = "WATCH"
    EXIT = "EXIT"
    REVERSE = "REVERSE"


@dataclass
class TradeThesis:
    direction: str
    entry: float
    h4_trend: str = ""
    bos: bool = False
    fvg: bool = False
    liquidity: bool = False
    gemini_decision: str = ""
    confidence: float = 0.0
    notes: list = field(default_factory=list)
    structure: dict = field(default_factory=dict)
    candle_id: str = ""
    reason: str = ""
    sl: float | None = None
    tp1: float | None = None
    tp2: float | None = None


@dataclass
class ActiveTrade:
    thesis: TradeThesis
    state: TradeState = TradeState.ACTIVE
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    history: list = field(default_factory=list)

    def transition(self, state: TradeState, reason: str) -> None:
        self.state = state
        self.history.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "state": state.value,
            "reason": reason,
        })

    def snapshot(self) -> dict:
        return {
            "thesis": asdict(self.thesis),
            "state": self.state.value,
            "created_at": self.created_at,
            "history": list(self.history),
        }


class TradeIntelligence:
    """Canonical lifecycle coordinator for the single active XAUUSD trade."""

    def __init__(self, store: TradeStateStore | None = None):
        self.store = store or TradeStateStore()
        self.active_trade: ActiveTrade | None = self._restore()

    def _restore(self) -> ActiveTrade | None:
        payload = self.store.load()
        if not payload:
            return None
        active = payload.get("active_trade")
        if not active:
            return None
        try:
            thesis_data = dict(active.get("thesis") or {})
            thesis = TradeThesis(**thesis_data)
            state = TradeState(active.get("state", TradeState.ACTIVE.value))
            return ActiveTrade(
                thesis=thesis,
                state=state,
                created_at=str(active.get("created_at") or datetime.now(timezone.utc).isoformat()),
                history=list(active.get("history") or []),
            )
        except (TypeError, ValueError):
            return None

    def _persist(self) -> None:
        self.store.save({
            "version": 1,
            "active_trade": self.active_trade.snapshot() if self.active_trade else None,
        })

    def snapshot(self) -> dict:
        return {
            "version": 1,
            "active_trade": self.active_trade.snapshot() if self.active_trade else None,
        }

    def has_active_trade(self) -> bool:
        return bool(self.active_trade and self.active_trade.state != TradeState.CLOSED)

    def can_open_new_trade(self) -> bool:
        return not self.has_active_trade()

    def open_trade(self, thesis: TradeThesis) -> bool:
        if not self.can_open_new_trade():
            return False
        self.active_trade = ActiveTrade(thesis=thesis)
        self.active_trade.history.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "state": "OPENED",
            "reason": "trade thesis accepted",
        })
        self._persist()
        return True

    def review(self, market: dict) -> ReviewDecision:
        trade = self.active_trade
        if not trade or trade.state == TradeState.CLOSED:
            return ReviewDecision.KEEP

        if market.get("structure_break_against") or market.get("invalidation"):
            trade.transition(TradeState.INVALIDATED, "thesis invalidated")
            self._persist()
            return ReviewDecision.REVERSE

        if market.get("reversal_signal"):
            trade.transition(TradeState.REVERSAL_PENDING, "reversal conditions detected")
            self._persist()
            return ReviewDecision.REVERSE

        if market.get("tp1_reached"):
            trade.transition(TradeState.TP1_REACHED, "first objective reached")
            self._persist()
            return ReviewDecision.KEEP

        if market.get("temporary_pressure"):
            trade.transition(TradeState.UNDER_PRESSURE, "temporary adverse movement")
            self._persist()
            return ReviewDecision.WATCH

        return ReviewDecision.KEEP

    def manage(self, market: dict) -> dict:
        decision = self.review(market)
        state = self.active_trade.state if self.active_trade else TradeState.NO_POSITION
        management = {
            "move_sl_to_breakeven": state == TradeState.TP1_REACHED,
            "trail_stop": state == TradeState.TP1_REACHED,
            "reduce_risk": state == TradeState.UNDER_PRESSURE,
            "exit_now": state == TradeState.INVALIDATED,
            "close_current_before_reversal": state == TradeState.REVERSAL_PENDING,
            "reverse_candidate": state in {TradeState.INVALIDATED, TradeState.REVERSAL_PENDING},
        }
        return {
            "decision": decision.value,
            "state": state.value,
            "management": management,
        }

    def close(self, reason: str) -> None:
        if not self.active_trade:
            return
        if self.active_trade.state != TradeState.CLOSED:
            self.active_trade.transition(TradeState.CLOSED, reason)
        self._persist()


trade_intelligence = TradeIntelligence()

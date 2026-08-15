"""Active Trade Intelligence lifecycle engine."""
from dataclasses import dataclass, field, asdict
from enum import Enum
from datetime import datetime, timezone


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


@dataclass
class ActiveTrade:
    thesis: TradeThesis
    state: TradeState = TradeState.ACTIVE
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    history: list = field(default_factory=list)

    def transition(self, state, reason):
        self.state = state
        self.history.append({"time": datetime.now(timezone.utc).isoformat(), "state": state.value, "reason": reason})

    def snapshot(self):
        return {"thesis": asdict(self.thesis), "state": self.state.value, "created_at": self.created_at, "history": self.history}


class TradeIntelligence:
    def __init__(self):
        self.active_trade = None

    def can_open_new_trade(self):
        return self.active_trade is None or self.active_trade.state == TradeState.CLOSED

    def open_trade(self, thesis):
        if not self.can_open_new_trade():
            return False
        self.active_trade = ActiveTrade(thesis=thesis)
        self.active_trade.history.append({"time": datetime.now(timezone.utc).isoformat(), "state": "OPENED", "reason": "trade thesis accepted"})
        return True

    def review(self, market):
        trade = self.active_trade
        if not trade:
            return ReviewDecision.KEEP
        if market.get("structure_break_against") or market.get("invalidation"):
            trade.transition(TradeState.INVALIDATED, "thesis invalidated")
            return ReviewDecision.REVERSE
        if market.get("reversal_signal"):
            trade.transition(TradeState.REVERSAL_PENDING, "reversal conditions detected")
            return ReviewDecision.REVERSE
        if market.get("temporary_pressure"):
            trade.transition(TradeState.UNDER_PRESSURE, "temporary adverse movement")
            return ReviewDecision.WATCH
        if market.get("tp1_reached"):
            trade.transition(TradeState.TP1_REACHED, "first objective reached")
        return ReviewDecision.KEEP

    def manage(self, market):
        decision = self.review(market)
        return {"decision": decision.value, "state": self.active_trade.state.value if self.active_trade else TradeState.NO_POSITION.value}

    def close(self, reason):
        if self.active_trade:
            self.active_trade.transition(TradeState.CLOSED, reason)


trade_intelligence = TradeIntelligence()

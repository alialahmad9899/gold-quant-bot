"""Phase 2 Active Trade Intelligence runtime integration helpers.

Provides orchestration primitives used by the signal pipeline: position lock,
review loop, invalidation/reversal evaluation, persistence, thesis logging and
dynamic management.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock


@dataclass
class TradeThesisRecord:
    direction: str
    entry: float
    reason: str
    structure: dict
    created_at: str


class ActiveTradeManager:
    def __init__(self, state_file="active_trade_state.json"):
        self.lock = Lock()
        self.path = Path(state_file)
        self.state = self._load()

    def _load(self):
        if not self.path.exists():
            return {"active": None, "history": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"active": None, "history": []}

    def _save(self):
        self.path.write_text(json.dumps(self.state, indent=2, default=str), encoding="utf-8")

    def has_active_position(self):
        return self.state.get("active") is not None

    def open(self, thesis: TradeThesisRecord):
        with self.lock:
            if self.has_active_position():
                return False
            self.state["active"] = asdict(thesis)
            self.state["history"].append({"event": "OPEN", "time": datetime.now(timezone.utc).isoformat()})
            self._save()
            return True

    def review(self, market):
        with self.lock:
            trade = self.state.get("active")
            if not trade:
                return "NO_POSITION"
            if market.get("invalidate"):
                return self._transition("INVALIDATED")
            if market.get("reverse"):
                return self._transition("REVERSAL_PENDING")
            if market.get("tp1"):
                return self._transition("TP1_REACHED")
            return "KEEP"

    def _transition(self, state):
        self.state["active"]["state"] = state
        self.state["history"].append({"event": state, "time": datetime.now(timezone.utc).isoformat()})
        self._save()
        return state

    def close(self, reason="closed"):
        with self.lock:
            self.state["history"].append({"event": "CLOSE", "reason": reason})
            self.state["active"] = None
            self._save()


active_trade_manager = ActiveTradeManager()

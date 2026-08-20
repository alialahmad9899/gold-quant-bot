"""Runtime coordinator that connects live news to signal and active-trade loops."""
from __future__ import annotations

import time
from typing import Any

from news_intelligence import NewsDecision, NewsIntelligence


class NewsRuntimeCoordinator:
    """Polls news at a bounded cadence and exposes one normalized decision snapshot."""

    def __init__(self, engine: NewsIntelligence | None = None):
        self.engine = engine or NewsIntelligence()
        self.last_price: float | None = None
        self.last_poll_monotonic = 0.0
        self.last_snapshot: dict[str, Any] = {
            "fresh_count": 0,
            "decision": {"action": "NO_TRADE", "direction": "NEUTRAL", "impact": 0, "confidence": 0, "urgency": "LOW", "reason": "لا يوجد خبر جديد."},
            "decisions": [],
            "updated_at": None,
        }

    @staticmethod
    def _price_move(previous: float | None, current: float | None) -> tuple[float, str]:
        if previous is None or current is None or previous <= 0 or current <= 0:
            return 0.0, "FLAT"
        pct = (current - previous) / previous * 100.0
        return abs(pct), "UP" if pct > 0 else "DOWN" if pct < 0 else "FLAT"

    @staticmethod
    def _decision_dict(decision: NewsDecision) -> dict[str, Any]:
        return {
            "action": decision.action,
            "direction": decision.direction,
            "impact": decision.impact,
            "confidence": decision.confidence,
            "urgency": decision.urgency,
            "reason": decision.reason,
            "conflict": decision.conflict,
            "article": decision.article,
        }

    @staticmethod
    def _strongest(decisions: list[NewsDecision]) -> NewsDecision | None:
        if not decisions:
            return None
        priority = {"EXIT": 6, "REDUCE_RISK": 5, "NEWS_BUY": 4, "NEWS_SELL": 4, "WAIT_CONFIRMATION": 3, "REASSESS": 2, "NO_TRADE": 1}
        return max(decisions, key=lambda d: (priority.get(d.action, 0), d.impact, d.confidence))

    def poll(self, *, price: float | None = None, price_change_pct: float | None = None, price_direction: str | None = None, active_direction: str | None = None, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self.last_poll_monotonic and now - self.last_poll_monotonic < self.engine.poll_seconds:
            return self.last_snapshot

        if price_change_pct is None or price_direction is None:
            price_change_pct, price_direction = self._price_move(self.last_price, price)
        articles = self.engine.fetch_latest()
        decisions: list[NewsDecision] = []
        for article in articles:
            if active_direction:
                decisions.append(self.engine.evaluate_active_trade(active_direction, article, price_change_pct, price_direction or "FLAT"))
            else:
                decisions.append(self.engine.evaluate_news_entry(article, price_change_pct, price_direction or "FLAT"))

        strongest = self._strongest(decisions)
        self.last_poll_monotonic = now
        if price is not None and price > 0:
            self.last_price = float(price)
        self.last_snapshot = {
            "fresh_count": len(articles),
            "decision": self._decision_dict(strongest) if strongest else {"action": "NO_TRADE", "direction": "NEUTRAL", "impact": 0, "confidence": 0, "urgency": "LOW", "reason": "لا يوجد خبر جديد."},
            "decisions": [self._decision_dict(item) for item in decisions],
            "updated_at": time.time(),
        }
        return self.last_snapshot

    def active_trade_context(self, *, price: float | None, active_direction: str) -> dict[str, Any]:
        snapshot = self.poll(price=price, active_direction=active_direction)
        return {"news_decision": snapshot["decision"], "news_decisions": snapshot["decisions"], "news_fresh_count": snapshot["fresh_count"]}

    def entry_context(self, *, price: float | None, force: bool = False) -> dict[str, Any]:
        snapshot = self.poll(price=price, force=force)
        return {"news_decision": snapshot["decision"], "news_decisions": snapshot["decisions"], "news_fresh_count": snapshot["fresh_count"]}

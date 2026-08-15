"""Twelve Data health state helpers.
Keeps data availability decisions explicit without affecting analysis engines.
"""
from enum import Enum
from dataclasses import dataclass


class TwelveDataState(str, Enum):
    LIVE_HEALTHY = "LIVE_HEALTHY"
    LIVE_STALE = "LIVE_STALE"
    HISTORICAL_HEALTHY = "HISTORICAL_HEALTHY"
    QUOTA_CONSERVATION = "QUOTA_CONSERVATION"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    TRADING_BLOCKED = "TRADING_BLOCKED"


@dataclass
class MarketHealth:
    state: TwelveDataState
    live_quote_age: float | None = None
    m15_age: float | None = None
    h1_age: float | None = None
    trading_allowed: bool = False
    reason: str = ""


def can_trade(health: MarketHealth) -> bool:
    return bool(
        health.trading_allowed
        and health.state == TwelveDataState.LIVE_HEALTHY
    )

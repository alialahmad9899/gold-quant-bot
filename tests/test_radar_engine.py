import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar_engine import MarketSnapshot, market_score


def make(**kwargs):
    values = dict(
        chain="ethereum", address="0xabc", pair_address="0xpair",
        symbol="TEST", name="Test", price_usd=0.001,
        market_cap=1_000_000, fdv=1_000_000, liquidity_usd=200_000,
        volume_5m=10_000, volume_1h=100_000, volume_6h=250_000,
        volume_24h=1_000_000, buys_1h=700, sells_1h=300,
        buys_24h=5000, sells_24h=3500, price_m5=2, price_h1=12,
        price_h6=18, price_h24=25, pair_age_hours=72, boosted=0,
    )
    values.update(kwargs)
    return MarketSnapshot(**values)


def test_score_rewards_buyer_pressure_and_liquidity():
    strong = market_score(make())
    weak = market_score(make(liquidity_usd=5_000, buys_1h=100, sells_1h=300, price_h1=-4))
    assert strong > weak


def test_vertical_pump_is_not_maximum_score():
    early = market_score(make(price_h1=18, price_m5=2))
    vertical = market_score(make(price_h1=160, price_m5=25))
    assert early > vertical
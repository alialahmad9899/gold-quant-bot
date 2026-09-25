import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar_engine import GoPlusClient, MarketSnapshot, MoralisClient, candidate_signal, market_score


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


def test_candidate_signal_requires_security_for_buy_watch():
    base = {
        "score": 88,
        "security_status": "PASS",
        "data_completeness": 1.0,
        "risk_flags": [],
        "smart_money": {"available": True, "net_flow_usd": 25_000},
        "market": {
            "buys_1h": 700, "sells_1h": 300,
            "volume_acceleration": 1.8, "price_h1": 12,
            "liquidity_usd": 250_000,
        },
    }
    assert candidate_signal(base) == "BUY_WATCH"
    base["security_status"] = "UNKNOWN"
    assert candidate_signal(base) != "BUY_WATCH"


def test_goplus_extracts_nested_token_result():
    address = "0xABCDEF1234567890"
    payload = {
        "code": 1,
        "message": "ok",
        "result": {
            address: {
                "is_honeypot": "0",
                "blacklist": "0",
                "is_open_source": "1",
                "buy_tax": "1",
                "sell_tax": "2",
            }
        },
    }
    assert GoPlusClient._extract_token_result(payload, address)["sell_tax"] == "2"


class FakeHTTP:
    def __init__(self):
        self.calls = []

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        if "/top-gainers" in url:
            return {"result": [{"address": "0xwallet", "totalPnlUsd": 1234}]}
        return []


def test_moralis_uses_current_token_top_gainers_endpoint():
    http = FakeHTTP()
    client = MoralisClient(http, "secret")
    rows = client.top_traders("ethereum", "0xtoken")
    assert rows and rows[0]["address"] == "0xwallet"
    assert "/erc20/0xtoken/top-gainers" in http.calls[0][0]
    assert http.calls[0][1]["chain"] == "eth"

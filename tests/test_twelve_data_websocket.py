import importlib
import json


def _runtime(monkeypatch):
    monkeypatch.setenv("CI", "true")
    gateway = importlib.import_module("twelve_data_gateway")
    runtime = importlib.import_module("sitecustomize")
    return gateway, runtime


def test_websocket_subscription_and_tick_do_not_consume_quota(monkeypatch):
    gateway, runtime = _runtime(monkeypatch)
    gateway.reset_for_tests()
    assert runtime.websocket_subscription_message() == {
        "action": "subscribe",
        "params": {"symbols": "XAU/USD"},
    }
    assert runtime.update_websocket_quote(json.dumps({
        "event": "price",
        "symbol": "XAU/USD",
        "price": "3342.50",
        "timestamp": "2026-08-14T00:00:00Z",
    }))
    quote = runtime.get_websocket_quote(10)
    assert quote["mid"] == 3342.5
    quota = gateway.quota_summary()
    assert quota["background_used"] == 0
    assert quota["manual_used"] == 0


def test_background_budget_exhaustion_keeps_manual_reserve(monkeypatch):
    gateway, runtime = _runtime(monkeypatch)
    gateway.reset_for_tests()
    gateway.MINUTE_BUDGET = 1000
    gateway._STATE["background_used"] = 750
    assert runtime.background_budget_available() is False
    assert runtime.background_budget_remaining() == 0
    assert gateway.reserve_credit("manual", "manual-test", 0) is True
    assert gateway._STATE["manual_used"] == 1


def test_background_cycle_consumes_one_credit_per_timeframe(monkeypatch):
    gateway, runtime = _runtime(monkeypatch)
    gateway.reset_for_tests()
    gateway.MINUTE_BUDGET = 1000
    seen = []

    def fake_fetch(interval):
        seen.append(interval)
        return None

    assert runtime.run_background_cycle(fake_fetch) == 3
    assert seen == ["5min", "15min", "1h"]
    assert gateway._STATE["background_used"] == 3


def test_websocket_quote_is_rejected_when_invalid(monkeypatch):
    _, runtime = _runtime(monkeypatch)
    assert runtime.update_websocket_quote({"event": "price", "symbol": "XAU/USD", "price": -1}) is False
    assert runtime.update_websocket_quote({"event": "price", "symbol": "EUR/USD", "price": 1.0}) is False

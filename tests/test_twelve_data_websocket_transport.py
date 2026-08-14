import importlib
import json


def runtime(monkeypatch):
    monkeypatch.setenv("CI", "true")
    gateway = importlib.import_module("twelve_data_gateway")
    module = importlib.import_module("sitecustomize")
    return gateway, module


def test_websocket_transport_bypasses_proxy_for_twelve_data(monkeypatch):
    _, module = runtime(monkeypatch)
    options = module.websocket_connection_options()
    assert "http_no_proxy" in options
    assert "ws.twelvedata.com" in options["http_no_proxy"]


def test_heartbeat_message_contract():
    module = importlib.import_module("sitecustomize")
    assert module.websocket_heartbeat_message() == {"action": "heartbeat"}


def test_subscribe_status_is_tracked_and_subscription_errors_are_visible(monkeypatch):
    _, module = runtime(monkeypatch)
    payload = {
        "event": "subscribe-status",
        "status": "ok",
        "success": [{"symbol": "XAU/USD"}],
        "fails": [],
    }
    assert module.parse_twelve_data_websocket_message(json.dumps(payload)) is True
    state = module.websocket_status()
    assert state["subscription_status"] == "ok"
    assert state["subscribed_symbols"] == ["XAU/USD"]

    failed = {
        "event": "subscribe-status",
        "status": "error",
        "success": [],
        "fails": [{"symbol": "XAU/USD", "reason": "not available"}],
    }
    assert module.parse_twelve_data_websocket_message(json.dumps(failed)) is False
    state = module.websocket_status()
    assert state["subscription_status"] == "error"
    assert state["status"] == "SUBSCRIPTION_ERROR"
    assert "not available" in state["last_error"]


def test_fresh_price_tick_populates_live_quote_cache(monkeypatch):
    _, module = runtime(monkeypatch)
    assert module.parse_twelve_data_websocket_message(json.dumps({
        "event": "price",
        "symbol": "XAU/USD",
        "timestamp": 1786665600,
        "price": 3342.75,
    })) is True
    quote = module.get_websocket_quote(max_age_seconds=10)
    assert quote is not None
    assert quote["price"] == 3342.75
    assert quote["mid"] == 3342.75
    assert quote["provider"] == "Twelve Data WebSocket"

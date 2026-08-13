import importlib


def test_twelve_data_budget_is_750_plus_50():
    gateway = importlib.import_module("twelve_data_gateway")
    assert gateway.BACKGROUND_BUDGET == 750
    assert gateway.MANUAL_RESERVE == 50
    assert gateway.TOTAL_BUDGET == 800


def test_background_cannot_cross_750(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_FILE", str(tmp_path / "quota.db"))
    gateway = importlib.import_module("twelve_data_gateway")
    gateway.reset_for_tests()
    gateway.MINUTE_BUDGET = 100
    gateway._STATE["background_used"] = 750
    assert gateway.reserve_credit("background", "background-overflow", 0) is False


def test_manual_reserve_can_reach_800(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_FILE", str(tmp_path / "quota.db"))
    gateway = importlib.import_module("twelve_data_gateway")
    gateway.reset_for_tests()
    gateway.MINUTE_BUDGET = 100
    gateway._STATE["background_used"] = 750
    gateway._STATE["manual_used"] = 49
    assert gateway.reserve_credit("manual", "manual-last", 0) is True
    assert gateway._STATE["background_used"] + gateway._STATE["manual_used"] == 800
    assert gateway.reserve_credit("manual", "manual-overflow", 0) is False


def test_failed_request_blocks_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_FILE", str(tmp_path / "quota.db"))
    gateway = importlib.import_module("twelve_data_gateway")
    gateway.reset_for_tests()
    gateway.MINUTE_BUDGET = 100
    now = 1000.0
    old = gateway.time.monotonic
    gateway.time.monotonic = lambda: now
    try:
        gateway.record_failure("429 Too Many Requests")
        assert gateway._STATE["blocked_until"] > now
        assert gateway.reserve_credit("background", "blocked", 0) is False
    finally:
        gateway.time.monotonic = old

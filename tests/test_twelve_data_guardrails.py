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


def test_manual_scope_classifies_quote_and_time_series_as_manual(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_FILE", str(tmp_path / "quota.db"))
    gateway = importlib.import_module("twelve_data_gateway")
    quote_url = "https://api.twelvedata.com/quote?symbol=XAU/USD&apikey=test"
    series_url = "https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=15min&apikey=test"

    assert gateway.classify_url(quote_url)[0] == "background"
    assert gateway.classify_url(series_url)[0] == "background"
    with gateway.request_class_scope("manual"):
        assert gateway.classify_url(quote_url)[0] == "manual"
        assert gateway.classify_url(series_url)[0] == "manual"


def test_manual_scope_survives_async_to_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_FILE", str(tmp_path / "quota.db"))
    gateway = importlib.import_module("twelve_data_gateway")
    url = "https://api.twelvedata.com/quote?symbol=XAU/USD&apikey=test"

    async def probe():
        import asyncio
        with gateway.request_class_scope("manual"):
            return await asyncio.to_thread(lambda: gateway.classify_url(url)[0])

    import asyncio
    assert asyncio.run(probe()) == "manual"


def test_manual_handler_name_survives_async_to_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_FILE", str(tmp_path / "quota.db"))
    gateway = importlib.import_module("twelve_data_gateway")
    url = "https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=15min&apikey=test"

    async def price():
        import asyncio
        return await asyncio.to_thread(lambda: gateway.classify_url(url)[0])

    import asyncio
    assert asyncio.run(price()) == "manual"

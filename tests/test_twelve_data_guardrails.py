import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BOT_PATH = Path(__file__).resolve().parents[1] / "bot.py"
spec = importlib.util.spec_from_file_location("gold_quant_bot", BOT_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

module._persist_twelve_data_state = lambda: None
module._load_twelve_data_persisted_state = lambda: None


def reset():
    module._reset_twelve_data_budget_for_tests()
    module.TWELVE_DATA_TOTAL_BUDGET = 800
    module.TWELVE_DATA_BACKGROUND_BUDGET = 750
    module.TWELVE_DATA_MANUAL_RESERVE = 50
    module.TWELVE_DATA_MINUTE_BUDGET = 4


def test_exact_budget_defaults():
    assert module.TWELVE_DATA_BACKGROUND_BUDGET == 750
    assert module.TWELVE_DATA_MANUAL_RESERVE == 50
    assert module.TWELVE_DATA_TOTAL_BUDGET == 800
    assert module.TWELVE_DATA_BACKGROUND_BUDGET + module.TWELVE_DATA_MANUAL_RESERVE == module.TWELVE_DATA_TOTAL_BUDGET


def test_background_stops_at_750():
    reset()
    module.TWELVE_DATA_STATE["daily_requests"] = 750
    assert not module._reserve_twelve_data_credit("background", "quote")
    assert module.TWELVE_DATA_STATE["daily_requests"] == 750


def test_manual_uses_reserved_tail_only():
    reset()
    module.TWELVE_DATA_STATE["daily_requests"] = 750
    allowed = sum(module._reserve_twelve_data_credit("manual", "quote") for _ in range(50))
    assert allowed == 50
    assert module.TWELVE_DATA_STATE["daily_requests"] == 800
    assert not module._reserve_twelve_data_credit("manual", "quote")


def test_background_cannot_touch_reserved_tail():
    reset()
    module.TWELVE_DATA_STATE["daily_requests"] = 799
    assert not module._reserve_twelve_data_credit("background", "quote")
    assert module.TWELVE_DATA_STATE["daily_requests"] == 799


def test_total_hard_ceiling_is_800():
    reset()
    module.TWELVE_DATA_STATE["daily_requests"] = 799
    assert module._reserve_twelve_data_credit("manual", "quote")
    assert module.TWELVE_DATA_STATE["daily_requests"] == 800
    assert not module._reserve_twelve_data_credit("manual", "quote")


def test_minute_budget_is_four():
    reset()
    base = 1000.0
    module.TWELVE_DATA_STATE["minute_requests"] = [base + i for i in range(4)]
    module.time.monotonic = lambda: base + 5
    assert not module._reserve_twelve_data_credit("manual", "quote")


def test_failed_request_enters_backoff():
    reset()
    now = 1000.0
    module.time.monotonic = lambda: now
    assert module._reserve_twelve_data_credit("background", "quote")
    module._record_twelve_data_failure("429 Too Many Requests")
    assert module.TWELVE_DATA_STATE["daily_requests"] == 1
    assert module.TWELVE_DATA_STATE["blocked_until"] > now
    assert not module._reserve_twelve_data_credit("background", "quote")


def test_concurrent_reservation_never_crosses_800():
    reset()
    module.TWELVE_DATA_STATE["daily_requests"] = 790
    module.TWELVE_DATA_STATE["minute_requests"] = []
    results = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        for result in executor.map(lambda _: module._reserve_twelve_data_credit("manual", "quote"), range(100)):
            results.append(result)
    assert sum(results) == 10
    assert module.TWELVE_DATA_STATE["daily_requests"] == 800


def test_quota_summary_reports_remaining_budget():
    reset()
    module.TWELVE_DATA_STATE["daily_requests"] = 742
    summary = module.twelve_data_quota_summary()
    assert summary["daily_used"] == 742
    assert summary["background_remaining"] == 8
    assert summary["daily_remaining"] == 58
    assert summary["manual_reserve"] == 50


def test_request_class_defaults_to_background():
    reset()
    assert module._reserve_twelve_data_credit("background", "quote")
    module.TWELVE_DATA_STATE["daily_requests"] = 750
    assert not module._twelve_data_request_allowed("quote")

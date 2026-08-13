"""Activate the repository Twelve Data guard at interpreter startup."""

import inspect
import requests
import twelve_data_gateway

_MANUAL_CALLERS = {
    "price",
    "analyze",
    "ai_info",
    "signal",
    "backtest",
    "system_health_check",
}


def _manual_call_active():
    try:
        return any(frame.function in _MANUAL_CALLERS for frame in inspect.stack(context=0)[1:])
    except Exception:
        return False


twelve_data_gateway.MINUTE_BUDGET = 4
twelve_data_gateway.is_manual_live_price_call = _manual_call_active

if not getattr(requests, "_gold_quant_twelve_data_guard_installed", False):
    requests.get = twelve_data_gateway.wrap_requests_get(requests.get)
    requests._gold_quant_twelve_data_guard_installed = True

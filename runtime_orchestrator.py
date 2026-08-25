"""Deterministic production bootstrap for the canonical XAU/USD runtime."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pandas as pd
import requests

LOGGER = logging.getLogger("XAUUSD_QuantBot.RuntimeOrchestrator")
_LOCK = threading.RLock()
_INSTALLED = False
_REQUEST_PATCHED = False
H1_OUTPUTSIZE = 1000
H4_REQUIRED_BARS = 200


def _is_xau_h1_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url))
        if "api.twelvedata.com" not in parsed.netloc:
            return False
        if parsed.path.rstrip("/").split("/")[-1] != "time_series":
            return False
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        symbol = str(query.get("symbol", "")).upper().replace("%2F", "/")
        interval = str(query.get("interval", "")).lower()
        return symbol == "XAU/USD" and interval in {"1h", "60min"}
    except Exception:
        return False


def _upgrade_h1_outputsize(url: str) -> str:
    parsed = urlparse(str(url))
    pairs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    try:
        current = int(pairs.get("outputsize", "0") or 0)
    except ValueError:
        current = 0
    if current >= H1_OUTPUTSIZE:
        return url
    pairs["outputsize"] = str(H1_OUTPUTSIZE)
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def _patch_requests() -> None:
    global _REQUEST_PATCHED
    if _REQUEST_PATCHED:
        return
    original = requests.get
    if getattr(original, "_xau_h1_history_upgrade", False):
        _REQUEST_PATCHED = True
        return

    def wrapped(url: str, *args: Any, **kwargs: Any):
        target = _upgrade_h1_outputsize(url) if _is_xau_h1_url(url) else url
        return original(target, *args, **kwargs)

    wrapped._xau_h1_history_upgrade = True
    requests.get = wrapped
    _REQUEST_PATCHED = True
    LOGGER.info("✅ XAU/USD H1 history guard installed: outputsize >= %s", H1_OUTPUTSIZE)


def _h4_from_h1(df: Any) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    x = df.copy().sort_index()
    if not isinstance(x.index, pd.DatetimeIndex):
        x.index = pd.to_datetime(x.index, utc=True, errors="coerce")
    x = x[~x.index.isna()]
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(x.columns):
        return pd.DataFrame()
    return pd.DataFrame({
        "Open": x["Open"].resample("4h", label="left", closed="left").first(),
        "High": x["High"].resample("4h", label="left", closed="left").max(),
        "Low": x["Low"].resample("4h", label="left", closed="left").min(),
        "Close": x["Close"].resample("4h", label="left", closed="left").last(),
    }).dropna()


def _patch_market_cache(bot: Any) -> None:
    original = getattr(bot, "get_chart_data_cached", None)
    if original is None or getattr(original, "_h4_history_guard", False):
        return

    def wrapped(*args: Any, **kwargs: Any):
        data = original(*args, **kwargs)
        if not isinstance(data, dict):
            return data
        h1 = data.get("df_gold_h1")
        h4 = _h4_from_h1(h1)
        if len(h4) >= H4_REQUIRED_BARS:
            return data
        LOGGER.warning("[H4_HISTORY] only %s H4 bars available; canonical H4 EMA50/EMA200 requires >= %s", len(h4), H4_REQUIRED_BARS)
        cache = getattr(bot, "GLOBAL_CACHE", None)
        if isinstance(cache, dict):
            cache["h4_history_health"] = {"bars": len(h4), "required": H4_REQUIRED_BARS, "valid": False}
        return data

    wrapped._h4_history_guard = True
    bot.get_chart_data_cached = wrapped


def _mark_h4_health(bot: Any) -> None:
    try:
        data = bot.get_chart_data_cached() or {}
        h4 = _h4_from_h1(data.get("df_gold_h1"))
        cache = getattr(bot, "GLOBAL_CACHE", None)
        if isinstance(cache, dict):
            cache["h4_history_health"] = {
                "bars": len(h4), "required": H4_REQUIRED_BARS,
                "valid": len(h4) >= H4_REQUIRED_BARS, "ema_fast": 50, "ema_slow": 200,
            }
    except Exception as exc:
        LOGGER.warning("[H4_HISTORY] health calculation failed: %s", exc)


def _wait_for_decision_layer(timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            import decision_layer
            if getattr(decision_layer, "_PATCHED", False):
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _existing_phase2(bot: Any):
    current = getattr(bot, "generate_quant_signal", None)
    owner = getattr(current, "__self__", None)
    if owner is not None and owner.__class__.__name__ == "Phase2RuntimeIntegration":
        return owner
    return None


def _install_phase2_and_execution(bot: Any) -> None:
    from phase2_runtime_integration import Phase2RuntimeIntegration
    integration = getattr(bot, "_phase2_runtime_integration", None) or _existing_phase2(bot)
    if integration is None:
        integration = Phase2RuntimeIntegration(bot)
        integration.install()
    bot._phase2_runtime_integration = integration

    import execution_bridge
    execution_bridge.install(bot)
    bot._execution_bridge = execution_bridge


def install(bot: Any, timeout: float = 120.0) -> bool:
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return True
    _patch_requests()
    if not _wait_for_decision_layer(timeout):
        LOGGER.error("❌ Runtime orchestration aborted: canonical decision layer never became ready.")
        return False
    try:
        _patch_market_cache(bot)
        _mark_h4_health(bot)

        # Production fixes wrap the canonical Decision Layer before Phase 2.
        import production_fix
        production_fix.install(bot)
        bot._production_fix = production_fix

        _install_phase2_and_execution(bot)
        with _LOCK:
            _INSTALLED = True
        LOGGER.info("✅ Deterministic runtime order installed: Decision -> ProductionFix -> Phase2 -> Execution")
        return True
    except Exception as exc:
        LOGGER.exception("❌ Deterministic runtime orchestration failed: %s", exc)
        return False


def start(bot: Any) -> threading.Thread:
    thread = threading.Thread(target=install, args=(bot,), name="deterministic-runtime-orchestrator", daemon=True)
    thread.start()
    return thread


def health() -> dict[str, Any]:
    with _LOCK:
        return {"installed": _INSTALLED, "request_patch": _REQUEST_PATCHED}

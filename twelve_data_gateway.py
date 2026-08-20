from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

try:
    import psycopg2
except Exception:  # pragma: no cover
    psycopg2 = None

TOTAL_BUDGET = 800
BACKGROUND_BUDGET = 750
MANUAL_RESERVE = 50
MINUTE_BUDGET = 8
BACKGROUND_QUOTE_INTERVAL = 140.0
MANUAL_QUOTE_INTERVAL = 15.0
M15_INTERVAL = 900.0
H1_INTERVAL = 3600.0
MAX_BACKOFF_SECONDS = 900.0

_REQUEST_CLASS = contextvars.ContextVar("twelve_data_request_class", default="background")
_MANUAL_CALLERS = {
    "price",
    "analyze",
    "ai_info",
    "signal",
    "backtest",
    "system_health_check",
}

_LOCK = threading.RLock()
_STATE = {
    "day": None,
    "background_used": 0,
    "manual_used": 0,
    "minute_requests": [],
    "blocked_until": 0.0,
    "backoff_seconds": 0.0,
    "last_error": None,
    "loaded": False,
    "last_request_by_key": {},
}


def _utc_day() -> int:
    return int(datetime.now(timezone.utc).strftime("%Y%m%d"))


def _manual_call_active() -> bool:
    try:
        return any(frame.function in _MANUAL_CALLERS for frame in inspect.stack(context=0)[1:])
    except Exception:
        return False


@contextmanager
def request_class_scope(request_class: str):
    """Temporarily mark Twelve Data traffic as manual or background."""
    value = "manual" if request_class == "manual" else "background"
    token = _REQUEST_CLASS.set(value)
    try:
        yield
    finally:
        _REQUEST_CLASS.reset(token)


def manual_request_handler(func):
    """Decorate an async user handler so all Twelve Data calls use manual quota."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        with request_class_scope("manual"):
            return await func(*args, **kwargs)
    return wrapper


def _is_manual_context() -> bool:
    return _REQUEST_CLASS.get() == "manual"


def _run_with_manual_scope(func, args, kwargs):
    with request_class_scope("manual"):
        return func(*args, **kwargs)


def _install_asyncio_to_thread_guard():
    if getattr(asyncio, "_gold_quant_twelve_data_context_guard_installed", False):
        return
    original = asyncio.to_thread

    @functools.wraps(original)
    async def guarded_to_thread(func, /, *args, **kwargs):
        if _is_manual_context() or _manual_call_active():
            return await original(_run_with_manual_scope, func, args, kwargs)
        return await original(func, *args, **kwargs)

    asyncio.to_thread = guarded_to_thread
    asyncio._gold_quant_twelve_data_context_guard_installed = True


def _is_postgres() -> bool:
    url = os.getenv("DATABASE_URL", "")
    return bool(url and str(url).lower().startswith(("postgresql://", "postgres://")) and psycopg2)


def _db_connection():
    if _is_postgres():
        url = str(os.environ["DATABASE_URL"])
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(url, sslmode="require", connect_timeout=5)
    conn = sqlite3.connect(os.getenv("DB_FILE", "trades.db"), timeout=15)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _ensure_db():
    conn = _db_connection()
    try:
        cur = conn.cursor()
        if _is_postgres():
            cur.execute("CREATE TABLE IF NOT EXISTS config (key VARCHAR(50) PRIMARY KEY, value BIGINT)")
        else:
            cur.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value INTEGER)")
        conn.commit()
    finally:
        conn.close()


def _read_int(cur, key: str):
    if _is_postgres():
        cur.execute("SELECT value FROM config WHERE key=%s", (key,))
    else:
        cur.execute("SELECT value FROM config WHERE key=?", (key,))
    row = cur.fetchone()
    try:
        return int(row[0]) if row and row[0] is not None else None
    except (TypeError, ValueError):
        return None


def _write_int(cur, key: str, value: int):
    if _is_postgres():
        cur.execute(
            "INSERT INTO config (key,value) VALUES (%s,%s) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (key, int(value)),
        )
    else:
        cur.execute("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)", (key, int(value)))


def _load_locked():
    if _STATE["loaded"]:
        return
    _ensure_db()
    conn = _db_connection()
    try:
        cur = conn.cursor()
        day = _read_int(cur, "twelve_data_quota_day")
        background = _read_int(cur, "twelve_data_background_used")
        manual = _read_int(cur, "twelve_data_manual_used")
    finally:
        conn.close()
    today = _utc_day()
    _STATE["day"] = today
    if day == today:
        _STATE["background_used"] = max(0, int(background or 0))
        _STATE["manual_used"] = max(0, int(manual or 0))
    else:
        _STATE["background_used"] = 0
        _STATE["manual_used"] = 0
    _STATE["loaded"] = True


def _persist_locked():
    conn = _db_connection()
    try:
        cur = conn.cursor()
        _write_int(cur, "twelve_data_quota_day", int(_STATE["day"]))
        _write_int(cur, "twelve_data_background_used", int(_STATE["background_used"]))
        _write_int(cur, "twelve_data_manual_used", int(_STATE["manual_used"]))
        conn.commit()
    finally:
        conn.close()


def _roll_day_locked():
    _load_locked()
    today = _utc_day()
    if _STATE["day"] == today:
        return
    _STATE.update({
        "day": today,
        "background_used": 0,
        "manual_used": 0,
        "minute_requests": [],
        "blocked_until": 0.0,
        "backoff_seconds": 0.0,
        "last_error": None,
        "last_request_by_key": {},
    })
    _persist_locked()


def reset_for_tests():
    with _LOCK:
        _STATE.update({
            "day": _utc_day(),
            "background_used": 0,
            "manual_used": 0,
            "minute_requests": [],
            "blocked_until": 0.0,
            "backoff_seconds": 0.0,
            "last_error": None,
            "loaded": True,
            "last_request_by_key": {},
        })
        _ensure_db()
        _persist_locked()


def _prune_minute_locked(now: float):
    _STATE["minute_requests"] = [ts for ts in _STATE["minute_requests"] if now - ts < 60.0]


def reserve_credit(request_class="background", request_key=None, min_interval=0.0):
    request_class = "manual" if request_class == "manual" else "background"
    with _LOCK:
        _roll_day_locked()
        now = time.monotonic()
        _prune_minute_locked(now)
        if now < float(_STATE["blocked_until"] or 0.0):
            return False
        if len(_STATE["minute_requests"]) >= MINUTE_BUDGET:
            return False
        total = int(_STATE["background_used"]) + int(_STATE["manual_used"])
        if total >= TOTAL_BUDGET:
            return False
        if request_class == "background" and _STATE["background_used"] >= BACKGROUND_BUDGET:
            return False
        if request_class == "manual" and _STATE["manual_used"] >= MANUAL_RESERVE:
            return False
        if request_key and min_interval > 0:
            previous = float(_STATE["last_request_by_key"].get(request_key, 0.0) or 0.0)
            if previous and now - previous < min_interval:
                return False
        _STATE["minute_requests"].append(now)
        if request_class == "manual":
            _STATE["manual_used"] += 1
        else:
            _STATE["background_used"] += 1
        if request_key:
            _STATE["last_request_by_key"][request_key] = now
        _persist_locked()
        return True


def record_failure(message):
    text = str(message or "")[:300]
    lowered = text.lower()
    with _LOCK:
        previous = float(_STATE["backoff_seconds"] or 0.0)
        if "429" in lowered or "too many requests" in lowered:
            base = 90.0
        elif "401" in lowered or "403" in lowered or "invalid_key" in lowered:
            base = 60.0
        else:
            base = 30.0
        delay = min(MAX_BACKOFF_SECONDS, max(base, previous * 2.0 if previous else base))
        _STATE["backoff_seconds"] = delay
        _STATE["blocked_until"] = time.monotonic() + delay
        _STATE["last_error"] = text


def record_success():
    with _LOCK:
        _STATE["backoff_seconds"] = 0.0
        _STATE["blocked_until"] = 0.0
        _STATE["last_error"] = None


def quota_summary():
    with _LOCK:
        _roll_day_locked()
        _prune_minute_locked(time.monotonic())
        background = int(_STATE["background_used"])
        manual = int(_STATE["manual_used"])
        return {
            "day": int(_STATE["day"]),
            "background_used": background,
            "background_budget": BACKGROUND_BUDGET,
            "background_remaining": max(0, BACKGROUND_BUDGET - background),
            "manual_used": manual,
            "manual_reserve": MANUAL_RESERVE,
            "manual_remaining": max(0, MANUAL_RESERVE - manual),
            "total_used": background + manual,
            "total_budget": TOTAL_BUDGET,
            "minute_used": len(_STATE["minute_requests"]),
            "minute_budget": MINUTE_BUDGET,
            "blocked_until": float(_STATE["blocked_until"] or 0.0),
            "last_error": _STATE["last_error"],
        }


def is_manual_live_price_call():
    return _manual_call_active()


def classify_url(url: str):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    endpoint = parsed.path.rsplit("/", 1)[-1]
    symbol = query.get("symbol", [""])[0].upper()
    interval = query.get("interval", [""])[0].lower()
    if endpoint == "time_series":
        key = f"time_series:{symbol}:{interval}"
        interval_seconds = M15_INTERVAL if interval == "15min" else H1_INTERVAL if interval in {"1h", "60min"} else 900.0
        request_class = "manual" if (_is_manual_context() or _manual_call_active()) else "background"
        return request_class, key, MANUAL_QUOTE_INTERVAL if request_class == "manual" else interval_seconds
    manual = _is_manual_context() or (_manual_call_active() and endpoint == "quote" and symbol == "XAU/USD")
    key = f"quote:{symbol}"
    return ("manual" if manual else "background"), key, (MANUAL_QUOTE_INTERVAL if manual else BACKGROUND_QUOTE_INTERVAL)


def blocked_response(message):
    try:
        import requests
        response = requests.Response()
        response.status_code = 429
        response.url = "https://api.twelvedata.com/"
        response._content = json.dumps({"status": "error", "code": 429, "message": message}).encode("utf-8")
        response.headers["content-type"] = "application/json"
        return response
    except Exception:
        return None


def wrap_requests_get(original_get):
    def guarded_get(url, *args, **kwargs):
        if not isinstance(url, str) or "api.twelvedata.com" not in url:
            return original_get(url, *args, **kwargs)
        request_class, key, interval = classify_url(url)
        if not reserve_credit(request_class, key, interval):
            return blocked_response("Twelve Data request deferred by the centralized 750+50 quota guard.")
        try:
            response = original_get(url, *args, **kwargs)
            error_message = None
            if getattr(response, "status_code", 200) >= 400:
                error_message = f"HTTP {response.status_code}"
            else:
                try:
                    data = response.json()
                    if isinstance(data, dict) and str(data.get("status", "")).lower() == "error":
                        error_message = str(data.get("message") or "Twelve Data API error")
                except Exception:
                    pass
            if error_message:
                record_failure(error_message)
            else:
                record_success()
            return response
        except Exception as exc:
            record_failure(exc)
            raise
    guarded_get.__name__ = getattr(original_get, "__name__", "get")
    return guarded_get


_install_asyncio_to_thread_guard()


def start_phase2_trade_intelligence_bootstrap() -> None:
    """Attach Phase 2 lifecycle protection to the already-importing bot process."""
    try:
        from phase2_runtime_integration import start_phase2_runtime_bootstrap
        start_phase2_runtime_bootstrap()
    except Exception:
        # Phase 2 must never prevent Twelve Data startup.
        pass


start_phase2_trade_intelligence_bootstrap()

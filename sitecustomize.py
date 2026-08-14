"""Activate the Twelve Data HTTP guard and WebSocket-first runtime at interpreter startup."""
from __future__ import annotations

import inspect
import json
import logging
import math
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests

import twelve_data_gateway

try:
    import websocket
except Exception:  # pragma: no cover
    websocket = None

logger = logging.getLogger("XAUUSD_QuantBot.WebSocket")

_MANUAL_CALLERS = {"price", "analyze", "ai_info", "signal", "backtest", "system_health_check"}
WS_ENDPOINT = "wss://ws.twelvedata.com/v1/quotes/price?apikey={api_key}"
WS_SYMBOL = "XAU/USD"
WS_STALE_SECONDS = float(os.getenv("TWELVE_DATA_WS_STALE_SECONDS", "10"))
WS_MAX_BACKOFF_SECONDS = 900.0
WS_HEARTBEAT_SECONDS = 10.0
WS_SOCKET_TIMEOUT_SECONDS = 10.0
BACKGROUND_TIMEFRAMES = (("5min", "df_gold_m5"), ("15min", "df_gold_m15"), ("1h", "df_gold_h1"))
BACKGROUND_CYCLE_REQUESTS = len(BACKGROUND_TIMEFRAMES)
BACKGROUND_MIN_INTERVAL_SECONDS = float(os.getenv("TWELVE_DATA_BACKGROUND_MIN_INTERVAL", "90"))
BACKGROUND_MAX_INTERVAL_SECONDS = float(os.getenv("TWELVE_DATA_BACKGROUND_MAX_INTERVAL", "900"))

_WS_LOCK = threading.RLock()
_RUNTIME_LOCK = threading.Lock()
_RUNTIME_STOP = threading.Event()
_WS_THREAD: threading.Thread | None = None
_BG_THREAD: threading.Thread | None = None
_WS_STATE: dict[str, Any] = {
    "status": "DISABLED",
    "symbol": WS_SYMBOL,
    "last_error": None,
    "last_connected_at": None,
    "last_tick_at": None,
    "last_event_at": None,
    "last_heartbeat_at": None,
    "reconnect_count": 0,
    "connection_attempts": 0,
    "subscription_status": "unknown",
    "subscribed_symbols": [],
    "subscription_fails": [],
    "transport": "websocket-client",
    "quote": None,
}


def _manual_call_active():
    try:
        return any(frame.function in _MANUAL_CALLERS for frame in inspect.stack(context=0)[1:])
    except Exception:
        return False


def websocket_request_url(api_key: str) -> str:
    key = str(api_key or "").strip()
    if not key:
        raise ValueError("Twelve Data API key is required for WebSocket")
    return WS_ENDPOINT.format(api_key=key)


def websocket_subscription_message() -> dict[str, Any]:
    return {"action": "subscribe", "params": {"symbols": WS_SYMBOL}}


def websocket_heartbeat_message() -> dict[str, str]:
    return {"action": "heartbeat"}


def websocket_connection_options() -> dict[str, Any]:
    """Return safe WSS options; bypass cloud HTTP proxies for Twelve Data by default."""
    options: dict[str, Any] = {
        "timeout": WS_SOCKET_TIMEOUT_SECONDS,
        "http_no_proxy": ["ws.twelvedata.com"],
    }
    if os.getenv("TWELVE_DATA_WS_USE_PROXY", "0") == "1":
        options.pop("http_no_proxy", None)
    return options


def _emit_ws_log(level: int, message: str, *args: Any) -> None:
    try:
        logger.log(level, message, *args)
    except Exception:
        pass
    if level >= logging.ERROR:
        try:
            rendered = message % args if args else message
            print(f"[TWELVE_DATA_WS] {rendered}", flush=True)
        except Exception:
            pass


def _parse_timestamp(value: Any) -> datetime:
    if value is None or value == "":
        return datetime.now(timezone.utc)
    try:
        numeric = float(value)
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def update_websocket_quote(message: str | bytes | dict[str, Any]) -> bool:
    try:
        payload = json.loads(message) if isinstance(message, (str, bytes, bytearray)) else dict(message)
    except (TypeError, ValueError):
        return False
    if str(payload.get("event", "")).lower() != "price":
        return False
    if str(payload.get("symbol", "")).upper() != WS_SYMBOL:
        return False
    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(price) or price <= 0:
        return False
    source_dt = _parse_timestamp(payload.get("timestamp"))
    received_dt = datetime.now(timezone.utc)
    quote = {
        "symbol": WS_SYMBOL,
        "provider": "Twelve Data WebSocket",
        "status": "ACTIVE",
        "price": price,
        "mid": price,
        "spot": price,
        "bid": None,
        "ask": None,
        "source_timestamp": source_dt.isoformat(),
        "timestamp": source_dt.isoformat(),
        "received_timestamp": received_dt.isoformat(),
        "age_seconds": 0.0,
        "spread_available": False,
    }
    with _WS_LOCK:
        _WS_STATE["quote"] = quote
        _WS_STATE["last_tick_at"] = received_dt.isoformat()
        _WS_STATE["last_event_at"] = received_dt.isoformat()
        _WS_STATE["status"] = "ACTIVE"
        _WS_STATE["last_error"] = None
    return True


def get_websocket_quote(max_age_seconds: float = WS_STALE_SECONDS) -> dict[str, Any] | None:
    with _WS_LOCK:
        quote = dict(_WS_STATE.get("quote") or {})
    if not quote:
        return None
    try:
        received = datetime.fromisoformat(str(quote["received_timestamp"]).replace("Z", "+00:00"))
        age = max(0.0, (datetime.now(timezone.utc) - received).total_seconds())
    except (KeyError, TypeError, ValueError):
        return None
    if age > float(max_age_seconds):
        return None
    quote["age_seconds"] = round(age, 3)
    quote["status"] = "ACTIVE"
    return quote


def websocket_status() -> dict[str, Any]:
    with _WS_LOCK:
        state = dict(_WS_STATE)
        state["quote"] = dict(state.get("quote") or {}) or None
        state["subscribed_symbols"] = list(state.get("subscribed_symbols") or [])
        state["subscription_fails"] = list(state.get("subscription_fails") or [])
        return state


def _handle_subscription_status(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status", "")).lower()
    success = payload.get("success") or []
    fails = payload.get("fails") or []
    subscribed = []
    for item in success:
        if isinstance(item, dict):
            symbol = str(item.get("symbol", "")).upper()
        else:
            symbol = str(item).upper()
        if symbol:
            subscribed.append(symbol)

    with _WS_LOCK:
        _WS_STATE["subscription_status"] = status or "unknown"
        _WS_STATE["subscribed_symbols"] = subscribed
        _WS_STATE["subscription_fails"] = list(fails) if isinstance(fails, list) else [fails]
        _WS_STATE["last_event_at"] = datetime.now(timezone.utc).isoformat()

    if status == "ok" and WS_SYMBOL in subscribed:
        with _WS_LOCK:
            _WS_STATE["status"] = "SUBSCRIBED"
            _WS_STATE["last_error"] = None
        _emit_ws_log(logging.INFO, "✅ WebSocket subscribed successfully: %s", WS_SYMBOL)
        return True

    detail = json.dumps(fails, ensure_ascii=False)[:500]
    with _WS_LOCK:
        _WS_STATE["status"] = "SUBSCRIPTION_ERROR"
        _WS_STATE["last_error"] = detail or "subscription rejected by Twelve Data"
    _emit_ws_log(logging.ERROR, "❌ WebSocket subscription rejected for %s: %s", WS_SYMBOL, detail or "unknown reason")
    return False


def parse_twelve_data_websocket_message(raw_message: str | bytes) -> bool:
    try:
        payload = json.loads(raw_message)
    except (TypeError, ValueError):
        with _WS_LOCK:
            _WS_STATE["last_error"] = "invalid websocket JSON payload"
        return False

    event = str(payload.get("event", "")).lower()
    now = datetime.now(timezone.utc).isoformat()
    with _WS_LOCK:
        _WS_STATE["last_event_at"] = now

    if event == "subscribe-status":
        return _handle_subscription_status(payload)
    if event == "heartbeat":
        with _WS_LOCK:
            _WS_STATE["last_heartbeat_at"] = now
            if _WS_STATE.get("subscription_status") == "ok":
                _WS_STATE["status"] = "SUBSCRIBED"
        return True
    if event == "price":
        return update_websocket_quote(payload)

    with _WS_LOCK:
        _WS_STATE["last_error"] = f"unknown websocket event: {event or 'missing'}"
    return False


def _set_ws_status(status: str, error: str | None = None) -> None:
    with _WS_LOCK:
        _WS_STATE["status"] = status
        _WS_STATE["last_error"] = str(error)[:500] if error else None


def _send_heartbeat(ws_app: Any) -> None:
    try:
        ws_app.send(json.dumps(websocket_heartbeat_message()))
        with _WS_LOCK:
            _WS_STATE["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        raise ConnectionError(f"heartbeat send failed: {type(exc).__name__}: {exc}") from exc


def _websocket_worker(stop_event: threading.Event) -> None:
    api_key = os.getenv("TWELVE_DATA_API_KEY", os.getenv("TWELVEDATA_API_KEY", "")).strip()
    if not api_key or len(api_key) < 8:
        _set_ws_status("DISABLED", "missing or invalid API key")
        _emit_ws_log(logging.ERROR, "❌ WebSocket disabled: Twelve Data API key missing/invalid")
        return
    if websocket is None:
        _set_ws_status("DISABLED", "websocket-client dependency unavailable")
        _emit_ws_log(logging.ERROR, "❌ WebSocket disabled: websocket-client dependency unavailable")
        return

    backoff = 2.0
    while not stop_event.is_set():
        ws_app = None
        try:
            with _WS_LOCK:
                _WS_STATE["connection_attempts"] = int(_WS_STATE.get("connection_attempts", 0)) + 1
                attempt = int(_WS_STATE["connection_attempts"])
                _WS_STATE["subscription_status"] = "pending"
                _WS_STATE["subscribed_symbols"] = []
                _WS_STATE["subscription_fails"] = []
            _set_ws_status("CONNECTING")
            _emit_ws_log(logging.INFO, "🔌 WebSocket connecting to Twelve Data (attempt=%s)", attempt)

            options = websocket_connection_options()
            ws_app = websocket.create_connection(
                websocket_request_url(api_key),
                **options,
            )
            with _WS_LOCK:
                _WS_STATE["status"] = "CONNECTED"
                _WS_STATE["last_connected_at"] = datetime.now(timezone.utc).isoformat()
                _WS_STATE["last_error"] = None
            _emit_ws_log(logging.INFO, "✅ WebSocket TCP/TLS connection established")

            ws_app.send(json.dumps(websocket_subscription_message()))
            _emit_ws_log(logging.INFO, "📡 WebSocket subscribe sent for %s", WS_SYMBOL)
            backoff = 2.0
            next_heartbeat = time.monotonic() + WS_HEARTBEAT_SECONDS

            while not stop_event.is_set():
                try:
                    raw = ws_app.recv()
                except Exception as exc:
                    name = type(exc).__name__.lower()
                    text = str(exc).lower()
                    if "timeout" in name or "timed out" in text or "timeout" in text:
                        if time.monotonic() >= next_heartbeat:
                            _send_heartbeat(ws_app)
                            next_heartbeat = time.monotonic() + WS_HEARTBEAT_SECONDS
                        continue
                    raise

                if raw in (None, "", b""):
                    raise ConnectionError("Twelve Data WebSocket closed the stream")

                handled = parse_twelve_data_websocket_message(raw)
                if not handled:
                    state = websocket_status()
                    if state.get("status") == "SUBSCRIPTION_ERROR":
                        raise ConnectionError(state.get("last_error") or "subscription rejected")

                if time.monotonic() >= next_heartbeat:
                    _send_heartbeat(ws_app)
                    next_heartbeat = time.monotonic() + WS_HEARTBEAT_SECONDS

        except Exception as exc:  # pragma: no cover
            with _WS_LOCK:
                _WS_STATE["reconnect_count"] = int(_WS_STATE.get("reconnect_count", 0)) + 1
            _set_ws_status("DISCONNECTED", f"{type(exc).__name__}: {exc}")
            _emit_ws_log(logging.ERROR, "⚠️ WebSocket disconnected: %s: %s | reconnect in %.1fs", type(exc).__name__, exc, backoff)
            stop_event.wait(timeout=backoff)
            backoff = min(WS_MAX_BACKOFF_SECONDS, backoff * 2.0)
        finally:
            if ws_app is not None:
                try:
                    ws_app.close()
                except Exception:
                    pass


def background_budget_remaining() -> int:
    return int(twelve_data_gateway.quota_summary()["background_remaining"])


def background_budget_available() -> bool:
    return background_budget_remaining() > 0


def _seconds_until_utc_reset(now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    tomorrow = (current + timedelta(days=1)).date()
    reset = datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc)
    return max(1.0, (reset - current).total_seconds())


def next_background_interval_seconds(now: datetime | None = None) -> float:
    remaining = background_budget_remaining()
    if remaining <= 0:
        return BACKGROUND_MAX_INTERVAL_SECONDS
    cycles = max(1, math.ceil(remaining / BACKGROUND_CYCLE_REQUESTS))
    raw = _seconds_until_utc_reset(now) / cycles
    return max(BACKGROUND_MIN_INTERVAL_SECONDS, min(BACKGROUND_MAX_INTERVAL_SECONDS, raw))


def _extract_ohlc(response: requests.Response) -> pd.DataFrame:
    if response.status_code != 200:
        return pd.DataFrame()
    try:
        data = response.json()
    except Exception:
        return pd.DataFrame()
    values = data.get("values", []) if isinstance(data, dict) else []
    if not values:
        return pd.DataFrame()
    df = pd.DataFrame(values)
    if "datetime" not in df.columns:
        return pd.DataFrame()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    df = df.set_index("datetime").sort_index()
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    for col in ("Open", "High", "Low", "Close"):
        if col not in df.columns:
            return pd.DataFrame()
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "Volume" in df.columns:
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0)
    else:
        df["Volume"] = 0.0
    return df.dropna(subset=["Open", "High", "Low", "Close"])


def _summarize_frame(df: pd.DataFrame) -> dict[str, Any]:
    if df is None or df.empty:
        return {"rows": 0, "last_close": None, "ema20": None, "ema50": None, "volatility": None}
    close = df["Close"].astype(float)
    returns = close.pct_change().dropna()
    return {
        "rows": int(len(df)),
        "last_close": round(float(close.iloc[-1]), 6),
        "ema20": round(float(close.ewm(span=min(20, len(close)), adjust=False).mean().iloc[-1]), 6),
        "ema50": round(float(close.ewm(span=min(50, len(close)), adjust=False).mean().iloc[-1]), 6),
        "volatility": round(float(returns.tail(20).std()), 8) if not returns.empty else 0.0,
    }


def _update_bot_cache(frames: dict[str, pd.DataFrame]) -> None:
    bot = sys.modules.get("bot")
    if bot is None or not hasattr(bot, "MARKET_DATA_CACHE"):
        return
    cache_lock = getattr(bot, "cache_lock", None)
    if cache_lock is None:
        return
    summaries: dict[str, Any] = {}
    with cache_lock:
        cache = bot.MARKET_DATA_CACHE
        cache.setdefault("df_gold_m5", pd.DataFrame())
        for interval, key in BACKGROUND_TIMEFRAMES:
            frame = frames.get(interval)
            if frame is not None and not frame.empty:
                cache[key] = frame
                summaries[interval] = _summarize_frame(frame)
        if summaries:
            cache["last_background_refresh"] = datetime.now(timezone.utc)
            global_cache = getattr(bot, "GLOBAL_CACHE", None)
            if isinstance(global_cache, dict):
                global_cache["background_analysis"] = summaries


def _fetch_time_series(interval: str, outputsize: int = 150) -> pd.DataFrame:
    api_key = os.getenv("TWELVE_DATA_API_KEY", os.getenv("TWELVEDATA_API_KEY", "")).strip()
    if not api_key:
        return pd.DataFrame()
    url = ("https://api.twelvedata.com/time_series"
           f"?symbol={WS_SYMBOL}&interval={interval}&outputsize={int(outputsize)}&apikey={api_key}")
    try:
        return _extract_ohlc(requests.get(url, timeout=8))
    except Exception:
        return pd.DataFrame()


def run_background_cycle(fetcher: Callable[[str], pd.DataFrame] | None = None) -> int:
    if not background_budget_available():
        return 0
    fetch = fetcher or _fetch_time_series
    fetched = 0
    frames: dict[str, pd.DataFrame] = {}
    for interval, _cache_key in BACKGROUND_TIMEFRAMES:
        if not background_budget_available():
            break
        frame = fetch(interval)
        fetched += 1
        if frame is not None and not frame.empty:
            frames[interval] = frame
    if frames:
        _update_bot_cache(frames)
    return fetched


def _background_worker(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        if sys.modules.get("bot") is None:
            stop_event.wait(2.0)
            continue
        if background_budget_available():
            run_background_cycle()
        stop_event.wait(next_background_interval_seconds())


def wrap_websocket_quote_get(guarded_get: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped_get(url: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(url, str):
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            endpoint = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            symbol = query.get("symbol", [""])[0].upper()
            if endpoint == "quote" and symbol == WS_SYMBOL:
                quote = get_websocket_quote()
                if quote:
                    response = requests.Response()
                    response.status_code = 200
                    response.url = url
                    response.headers["content-type"] = "application/json"
                    response._content = json.dumps({
                        "symbol": WS_SYMBOL,
                        "price": quote["price"],
                        "close": quote["price"],
                        "datetime": quote["source_timestamp"],
                        "timestamp": int(datetime.fromisoformat(quote["source_timestamp"]).timestamp()),
                    }).encode("utf-8")
                    return response
        return guarded_get(url, *args, **kwargs)
    wrapped_get.__name__ = getattr(guarded_get, "__name__", "get")
    return wrapped_get


def start_runtime() -> None:
    global _WS_THREAD, _BG_THREAD
    api_key = os.getenv("TWELVE_DATA_API_KEY", os.getenv("TWELVEDATA_API_KEY", "")).strip()
    if not api_key or len(api_key) < 8:
        return
    with _RUNTIME_LOCK:
        if _WS_THREAD is None or not _WS_THREAD.is_alive():
            _RUNTIME_STOP.clear()
            _WS_THREAD = threading.Thread(target=_websocket_worker, args=(_RUNTIME_STOP,), name="twelve-data-ws", daemon=True)
            _WS_THREAD.start()
            _emit_ws_log(logging.INFO, "🚀 Twelve Data WebSocket worker started")
        if _BG_THREAD is None or not _BG_THREAD.is_alive():
            _BG_THREAD = threading.Thread(target=_background_worker, args=(_RUNTIME_STOP,), name="twelve-data-background", daemon=True)
            _BG_THREAD.start()


def stop_runtime() -> None:
    _RUNTIME_STOP.set()
    _emit_ws_log(logging.INFO, "🛑 Twelve Data background runtime stop requested")


twelve_data_gateway.MINUTE_BUDGET = 4
twelve_data_gateway.is_manual_live_price_call = _manual_call_active

if not getattr(requests, "_gold_quant_twelve_data_guard_installed", False):
    guarded_get = twelve_data_gateway.wrap_requests_get(requests.get)
    requests.get = wrap_websocket_quote_get(guarded_get)
    requests._gold_quant_twelve_data_guard_installed = True

if os.getenv("TWELVE_DATA_RUNTIME_AUTOSTART", "1") == "1" and os.getenv("CI", "false").lower() != "true":
    start_runtime()

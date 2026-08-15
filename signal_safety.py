"""Hard execution guard for signal lifecycle, freshness and Gemini vetting."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

try:
    import psycopg2
except Exception:  # pragma: no cover
    psycopg2 = None

LOGGER = logging.getLogger("XAUUSD_QuantBot.SignalSafety")
_INSTALL_LOCK = threading.Lock()
_DB_LOCK = threading.RLock()
_TLS = threading.local()
_INSTALLED = False

MAX_SIGNAL_FEED_AGE_SECONDS = float(os.getenv("SIGNAL_MAX_PRICE_AGE_SECONDS", "120"))
SIGNAL_VETTING_CACHE_HOURS = float(os.getenv("SIGNAL_VETTING_CACHE_HOURS", "24"))


def _is_pg(bot: Any, conn: Any) -> bool:
    return bool(
        getattr(bot, "is_postgres", lambda: False)()
        and psycopg2 is not None
        and isinstance(conn, psycopg2.extensions.connection)
    )


def _now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_approval(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "approved", "approve", "موافق", "مقبول"}
    return False


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _signal_fingerprint(bot: Any, signal_data: dict[str, Any], market_summary: dict[str, Any]) -> str:
    signal_snapshot = {
        k: v for k, v in signal_data.items()
        if k not in {"candle_id", "signal_candle_time", "status", "gemini_note", "ai_score", "trade_id"}
    }
    lessons = []
    try:
        lessons = list(getattr(bot, "get_recent_gemini_insights")())
    except Exception:
        pass
    return _stable_hash({
        "signal": signal_snapshot,
        "market": dict(market_summary or {}),
        "lessons": lessons,
        "rules_version": "signal-veto-v2",
    })


def _ensure_database_guards(bot: Any) -> bool:
    """Create the persistent vetting cache and a database-level candle uniqueness guard."""
    with _DB_LOCK:
        conn = None
        try:
            conn = bot.get_db_connection()
            is_pg = _is_pg(bot, conn)
            cur = conn.cursor()

            if is_pg:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signal_vettings (
                        candle_id VARCHAR(120) PRIMARY KEY,
                        fingerprint VARCHAR(64) NOT NULL,
                        signal_type VARCHAR(16),
                        approved INTEGER NOT NULL,
                        reason TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute(
                    """
                    DELETE FROM trades t
                    USING trades d
                    WHERE t.candle_id IS NOT NULL
                      AND t.candle_id = d.candle_id
                      AND t.id > d.id
                    """
                )
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_candle_id ON trades(candle_id)")
            else:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signal_vettings (
                        candle_id TEXT PRIMARY KEY,
                        fingerprint TEXT NOT NULL,
                        signal_type TEXT,
                        approved INTEGER NOT NULL,
                        reason TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute(
                    """
                    DELETE FROM trades
                    WHERE candle_id IS NOT NULL
                      AND id NOT IN (
                          SELECT MIN(id) FROM trades
                          WHERE candle_id IS NOT NULL
                          GROUP BY candle_id
                      )
                    """
                )
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_candle_id ON trades(candle_id)")

            conn.commit()
            return True
        except Exception as exc:
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
            LOGGER.debug("[SIGNAL_SAFETY] database guard not ready yet: %s", exc)
            return False
        finally:
            if conn is not None:
                try:
                    bot.release_db_connection(conn)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass


def _start_database_guard_worker(bot: Any) -> None:
    def worker() -> None:
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            if _ensure_database_guards(bot):
                LOGGER.info("✅ [SIGNAL_SAFETY] Persistent candle/vetting guards are ready.")
                return
            time.sleep(1.0)
        LOGGER.error("❌ [SIGNAL_SAFETY] Could not initialize persistent signal guards within 90s.")

    threading.Thread(target=worker, name="signal-safety-db-guard", daemon=True).start()


def _query_vetting(bot: Any, *, candle_id: str | None = None, fingerprint: str | None = None):
    conn = None
    try:
        conn = bot.get_db_connection()
        is_pg = _is_pg(bot, conn)
        ph = "%s" if is_pg else "?"
        cur = conn.cursor()
        if candle_id:
            cur.execute(
                f"SELECT approved,reason,fingerprint FROM signal_vettings WHERE candle_id={ph} LIMIT 1",
                (candle_id,),
            )
        elif fingerprint:
            if is_pg:
                interval_literal = f"{SIGNAL_VETTING_CACHE_HOURS:g} hours"
                cur.execute(
                    f"SELECT approved,reason,fingerprint FROM signal_vettings WHERE fingerprint={ph} AND created_at >= NOW() - INTERVAL '{interval_literal}' ORDER BY created_at DESC LIMIT 1",
                    (fingerprint,),
                )
            else:
                cur.execute(
                    f"SELECT approved,reason,fingerprint FROM signal_vettings WHERE fingerprint={ph} AND datetime(created_at) >= datetime('now', ?) ORDER BY datetime(created_at) DESC LIMIT 1",
                    (fingerprint, f"-{SIGNAL_VETTING_CACHE_HOURS:g} hours"),
                )
        else:
            return None
        row = cur.fetchone()
        if not row:
            return None
        return {"approved": bool(row[0]), "reason": str(row[1] or ""), "fingerprint": str(row[2] or "")}
    except Exception as exc:
        LOGGER.warning("[SIGNAL_SAFETY] vetting cache read failed: %s", str(exc)[:300])
        return None
    finally:
        if conn is not None:
            try:
                bot.release_db_connection(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


def _store_vetting(bot: Any, *, candle_id: str, fingerprint: str, signal_type: str, approved: bool, reason: str) -> None:
    conn = None
    try:
        conn = bot.get_db_connection()
        is_pg = _is_pg(bot, conn)
        cur = conn.cursor()
        params = (_now_sql(), candle_id, fingerprint, signal_type, int(approved), reason[:2000])
        if is_pg:
            cur.execute(
                """
                INSERT INTO signal_vettings(created_at,candle_id,fingerprint,signal_type,approved,reason)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (candle_id) DO NOTHING
                """,
                params,
            )
        else:
            cur.execute(
                """
                INSERT OR IGNORE INTO signal_vettings(created_at,candle_id,fingerprint,signal_type,approved,reason)
                VALUES (?,?,?,?,?,?)
                """,
                params,
            )
        conn.commit()
    except Exception as exc:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        LOGGER.warning("[SIGNAL_SAFETY] vetting cache write failed: %s", str(exc)[:300])
    finally:
        if conn is not None:
            try:
                bot.release_db_connection(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


def _safe_price_feed(original_get, bot: Any):
    try:
        live = bot._twelve_data_runtime.get_websocket_quote(max_age_seconds=MAX_SIGNAL_FEED_AGE_SECONDS)
    except Exception:
        live = None

    if live:
        mid = live.get("mid") or live.get("price") or live.get("spot")
        if mid:
            value = float(mid)
            live = dict(live)
            live["status"] = "ACTIVE"
            live["bid"] = float(live.get("bid") or value)
            live["ask"] = float(live.get("ask") or value)
            live["mid"] = value
            live["spot"] = value
            live["signal_safe"] = True
            return live

    feed = original_get()
    if not feed:
        return feed

    safe = dict(feed)
    provider = str(safe.get("provider") or "")
    source_ts = safe.get("source_timestamp") or safe.get("timestamp")
    age = safe.get("age_seconds")
    if age is None and source_ts:
        try:
            parsed = datetime.fromisoformat(str(source_ts).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
        except Exception:
            age = None

    if "M15 Close" in provider:
        safe["status"] = "STALE"
        safe["signal_safe"] = False
        safe["error_type"] = "historical_fallback_blocked"
        safe["error_message"] = "Historical M15 close cannot authorize live signal execution."
        return safe

    if age is not None and float(age) > MAX_SIGNAL_FEED_AGE_SECONDS:
        safe["status"] = "STALE"
        safe["signal_safe"] = False
        safe["error_type"] = "live_feed_stale"
        safe["error_message"] = f"Live XAU/USD feed is {float(age):.1f}s old."
        return safe

    if safe.get("status") != "ACTIVE":
        safe["signal_safe"] = False
        return safe

    safe["signal_safe"] = True
    return safe


def _normalize_vetting_result(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"approved": False, "reason": "Gemini returned an invalid vetting payload."}
    approved = _normalize_approval(raw.get("approved", False))
    reason = str(raw.get("reason") or "").strip()
    if approved and any(marker in reason for marker in ("غير متاحة مؤقتاً", "غير مفعلة", "غير مهيأة", "اعتماد كمي تلقائي")):
        approved = False
        reason = "تعذر الحصول على مراجعة Gemini موثوقة؛ تم حظر الإشارة احتياطياً."
    if not reason:
        reason = "تم رفض الإشارة احتياطياً لغياب سبب موثوق من Gemini." if not approved else "تمت الموافقة على الإشارة."
    return {"approved": approved, "reason": reason}


def _install_gemini_guard(bot: Any) -> None:
    original = getattr(bot, "gemini_verify_signal", None)
    if original is None or getattr(original, "_signal_safety_wrapped", False):
        return

    def guarded(signal_data: dict[str, Any], market_summary: dict[str, Any]):
        candle_id = str(signal_data.get("candle_id") or "")
        fingerprint = _signal_fingerprint(bot, signal_data, market_summary)

        cached = _query_vetting(bot, candle_id=candle_id) if candle_id else None
        if cached is None:
            cached = _query_vetting(bot, fingerprint=fingerprint)
        if cached is not None:
            result = {"approved": bool(cached["approved"]), "reason": cached["reason"], "cached": True}
            _TLS.vetting = {"approved": bool(result["approved"]), "candle_id": candle_id, "fingerprint": fingerprint, "reason": result["reason"]}
            LOGGER.info("[SIGNAL_SAFETY] Reusing canonical Gemini decision: approved=%s candle=%s", result["approved"], candle_id)
            return result

        try:
            raw = original(signal_data, market_summary)
        except Exception as exc:
            raw = {"approved": False, "reason": f"تعذر تنفيذ مراجعة Gemini: {type(exc).__name__}: {exc}"}
        result = _normalize_vetting_result(raw)
        if candle_id:
            _store_vetting(
                bot,
                candle_id=candle_id,
                fingerprint=fingerprint,
                signal_type=str(signal_data.get("type") or ""),
                approved=bool(result["approved"]),
                reason=str(result["reason"]),
            )
        _TLS.vetting = {"approved": bool(result["approved"]), "candle_id": candle_id, "fingerprint": fingerprint, "reason": result["reason"]}
        LOGGER.info("[SIGNAL_SAFETY] Canonical Gemini decision: approved=%s candle=%s", result["approved"], candle_id)
        return result

    guarded._signal_safety_wrapped = True
    bot.gemini_verify_signal = guarded


def _install_trade_guard(bot: Any) -> None:
    original = getattr(bot, "log_trade", None)
    if original is None or getattr(original, "_signal_safety_wrapped", False):
        return

    def guarded_log_trade(*args: Any, **kwargs: Any):
        candle_id = kwargs.get("candle_id")
        if candle_id is None and len(args) >= 11:
            candle_id = args[10]
        candle_id = str(candle_id or "")

        vetting = getattr(_TLS, "vetting", None)
        if vetting and candle_id and vetting.get("candle_id") == candle_id and not vetting.get("approved", False):
            LOGGER.warning("[SIGNAL_SAFETY] HARD VETO: Gemini rejected candle=%s; trade insert blocked.", candle_id)
            return False, None

        if candle_id:
            conn = None
            try:
                conn = bot.get_db_connection()
                is_pg = _is_pg(bot, conn)
                ph = "%s" if is_pg else "?"
                cur = conn.cursor()
                cur.execute(f"SELECT id FROM trades WHERE candle_id={ph} LIMIT 1", (candle_id,))
                if cur.fetchone():
                    LOGGER.warning("[SIGNAL_SAFETY] Duplicate candle blocked before trade insert: %s", candle_id)
                    return False, None
            except Exception as exc:
                LOGGER.warning("[SIGNAL_SAFETY] Duplicate pre-check failed: %s", str(exc)[:250])
            finally:
                if conn is not None:
                    try:
                        bot.release_db_connection(conn)
                    except Exception:
                        try:
                            conn.close()
                        except Exception:
                            pass

        return original(*args, **kwargs)

    guarded_log_trade._signal_safety_wrapped = True
    bot.log_trade = guarded_log_trade


def _install_generation_guard(bot: Any) -> None:
    original = getattr(bot, "generate_quant_signal", None)
    if original is None or getattr(original, "_signal_safety_wrapped", False):
        return

    def guarded_generate(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        vetting = getattr(_TLS, "vetting", None)
        if isinstance(result, dict) and result.get("status") == "SIGNAL" and vetting and vetting.get("candle_id") == result.get("candle_id") and not vetting.get("approved", False):
            return {
                "status": "WAIT",
                "reason": f"🛑 مراجعة Gemini رفضت الإشارة، لذلك تم منع نشرها وتسجيلها كصفقة: {result.get('gemini_note') or 'القرار النهائي: رفض.'}",
                "price": result.get("entry") or result.get("price", 0.0),
                "candle_id": result.get("candle_id"),
            }
        if isinstance(result, dict) and result.get("status") == "WAIT" and vetting and not vetting.get("approved", True):
            reason = str(result.get("reason") or "")
            if "تمت معالجة هذه الشمعة مسبقاً" in reason:
                result = dict(result)
                result["reason"] = f"🛑 مراجعة Gemini رفضت الإشارة، وتم منع تسجيلها: {vetting.get('reason', reason)}"
        return result

    guarded_generate._signal_safety_wrapped = True
    bot.generate_quant_signal = guarded_generate


def _install_feed_guard(bot: Any) -> None:
    original = getattr(bot, "fetch_canonical_xauusd_feed", None)
    if original is None or getattr(original, "_signal_safety_wrapped", False):
        return
    if not hasattr(bot, "_twelve_data_runtime"):
        try:
            import sitecustomize as runtime
            bot._twelve_data_runtime = runtime
        except Exception:
            return

    def guarded_feed():
        return _safe_price_feed(original, bot)

    guarded_feed._signal_safety_wrapped = True
    bot.fetch_canonical_xauusd_feed = guarded_feed
    bot.fetch_live_spot_gold = lambda: float((bot.fetch_canonical_xauusd_feed() or {}).get("mid") or 0.0)


def install_signal_safety(bot: Any | None = None) -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        if bot is None:
            import sys
            bot = sys.modules.get("bot") or sys.modules.get("__main__")
        if bot is None:
            return
        _install_feed_guard(bot)
        _install_gemini_guard(bot)
        _install_trade_guard(bot)
        _install_generation_guard(bot)
        _start_database_guard_worker(bot)
        _INSTALLED = True
        LOGGER.info("🛡️ [SIGNAL_SAFETY] Feed freshness + AI hard-veto + persistent de-duplication enabled.")

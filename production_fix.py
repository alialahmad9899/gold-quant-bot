"""Final production hardening for the canonical XAU/USD runtime.

This module fixes runtime symptoms without adding another trading strategy:
- hard price/data sanity checks before a signal is considered trade-ready
- non-finite DXY values are normalized to UNKNOWN
- signal/ledger statistics are persisted independently from presentation
- Telegram buttons/commands are routed before generic text handlers
- Trade Lawyer uses the canonical active-trade state when available
"""
from __future__ import annotations

import logging
import math
import os
import threading
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger("XAUUSD_QuantBot.ProductionFix")
_LOCK = threading.RLock()
_INSTALLED = False
_APPLICATION_PATCHED = False
_GENERATION_PATCHED = False

PRICE_SANITY_MAX_PCT = float(os.getenv("FINAL_PRICE_SANITY_MAX_PCT", "0.0030"))


def _is_pg(bot: Any, conn: Any) -> bool:
    try:
        return bool(getattr(bot, "is_postgres", lambda: False)())
    except Exception:
        return False


def _ensure_schema(bot: Any) -> None:
    conn = None
    try:
        conn = bot.get_db_connection()
        cur = conn.cursor()
        if _is_pg(bot, conn):
            cur.execute("CREATE TABLE IF NOT EXISTS runtime_decision_events (id BIGSERIAL PRIMARY KEY, candle_id TEXT, direction TEXT, final_decision TEXT, decision_state TEXT, confidence REAL, entry REAL, live_price REAL, price_gap_pct REAL, ai_advisory BOOLEAN, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runtime_decision_events_created ON runtime_decision_events(created_at)")
        else:
            cur.execute("CREATE TABLE IF NOT EXISTS runtime_decision_events (id INTEGER PRIMARY KEY AUTOINCREMENT, candle_id TEXT, direction TEXT, final_decision TEXT, decision_state TEXT, confidence REAL, entry REAL, live_price REAL, price_gap_pct REAL, ai_advisory INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runtime_decision_events_created ON runtime_decision_events(created_at)")
        conn.commit()
    except Exception as exc:
        LOGGER.warning("runtime audit schema: %s", exc)
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                bot.release_db_connection(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


def _db_event(bot: Any, result: dict[str, Any], live_price: float | None, gap_pct: float | None) -> None:
    conn = None
    try:
        conn = bot.get_db_connection()
        cur = conn.cursor()
        pg = _is_pg(bot, conn)
        sql = "INSERT INTO runtime_decision_events(candle_id,direction,final_decision,decision_state,confidence,entry,live_price,price_gap_pct,ai_advisory) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)" if pg else "INSERT INTO runtime_decision_events(candle_id,direction,final_decision,decision_state,confidence,entry,live_price,price_gap_pct,ai_advisory) VALUES (?,?,?,?,?,?,?,?,?)"
        direction = "BUY" if "شراء" in str(result.get("type", "")) else "SELL" if "بيع" in str(result.get("type", "")) else str(result.get("direction") or "UNKNOWN")
        cur.execute(sql, (str(result.get("candle_id") or ""), direction, str(result.get("final_decision") or ""), str(result.get("decision_state") or ""), float(result.get("confidence") or 0.0), float(result.get("entry") or 0.0), live_price, gap_pct, bool(result.get("ai_advisory"))))
        conn.commit()
    except Exception as exc:
        LOGGER.warning("runtime decision audit write failed: %s", exc)
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                bot.release_db_connection(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


def _live_price(bot: Any) -> float | None:
    try:
        import sitecustomize
        quote = sitecustomize.get_websocket_quote()
        if quote and math.isfinite(float(quote.get("price"))):
            return float(quote["price"])
    except Exception:
        pass
    try:
        market = bot.get_market_data() or {}
        value = (market.get("price_feed") or {}).get("mid") or market.get("gold")
        value = float(value) if value is not None else None
        return value if value and math.isfinite(value) else None
    except Exception:
        return None


def _normalize_numeric(result: dict[str, Any]) -> None:
    for key in ("entry", "sl", "tp1", "tp2", "rr", "dxy_corr"):
        value = result.get(key)
        if value is None:
            continue
        try:
            if not math.isfinite(float(value)):
                result[key] = None
        except (TypeError, ValueError):
            result[key] = None
    if result.get("dxy_corr") is None:
        result["dxy_trend"] = "UNKNOWN"
        result["dxy_pressure"] = "NEUTRAL"
        result["dxy_data_quality"] = "UNKNOWN"


def _apply_price_sanity(bot: Any, result: dict[str, Any]) -> dict[str, Any]:
    _normalize_numeric(result)
    live = _live_price(bot)
    entry = result.get("entry")
    if live is not None and entry is not None:
        try:
            gap = abs(float(entry) - live) / live
            result["live_price_at_decision"] = round(live, 4)
            result["entry_price_gap_pct"] = round(gap * 100.0, 4)
            if gap > PRICE_SANITY_MAX_PCT:
                return {"status": "WAIT", "decision_state": "PRICE_SANITY_FAIL", "reason": f"سعر الإشارة قديم/غير متطابق مع السعر الحي: فرق {gap*100:.3f}% يتجاوز الحد الآمن {PRICE_SANITY_MAX_PCT*100:.3f}%.", "price": live, "candidate": False}
        except (TypeError, ValueError):
            return {"status": "WAIT", "decision_state": "PRICE_SANITY_FAIL", "reason": "تعذر التحقق من تطابق سعر الإشارة مع السعر الحي.", "price": live, "candidate": False}
    result.setdefault("candidate", True)
    result.setdefault("decision_state", "TRADE_READY")
    return result


def _patch_generation(bot: Any) -> None:
    global _GENERATION_PATCHED
    current = getattr(bot, "generate_quant_signal", None)
    if current is None or getattr(current, "_production_fix", False):
        _GENERATION_PATCHED = current is not None
        return

    def wrapped(*args: Any, **kwargs: Any):
        result = current(*args, **kwargs)
        if not isinstance(result, dict):
            return result
        if result.get("status") != "SIGNAL":
            return result
        result = dict(result)
        result = _apply_price_sanity(bot, result)
        if result.get("status") != "SIGNAL":
            return result
        confidence = float(result.get("confidence") or 0.0)
        max_score = max(float(result.get("score_bull") or 0.0), float(result.get("score_bear") or 0.0))
        if confidence < 40.0 and max_score < 6.0:
            return {"status": "WAIT", "decision_state": "WATCH", "candidate": True, "reason": f"فرصة تحت المراقبة فقط: الثقة {confidence:.0f}% مع قوة اتجاهية {max_score:.2f} غير كافية للدخول.", "price": result.get("entry", 0.0), "candle_id": result.get("candle_id")}
        if result.get("ai_advisory"):
            result["final_decision"] = "APPROVE_WITH_CAUTION"
            result["final_reason"] = "Gemini متحفظ؛ القرار الكمي سمح بالدخول بحذر بعد اجتياز فحوص البيانات والمخاطر."
        result["decision_state"] = "TRADE_READY"
        result["candidate"] = True
        live = result.get("live_price_at_decision")
        gap = result.get("entry_price_gap_pct")
        _db_event(bot, result, float(live) if live is not None else None, float(gap) if gap is not None else None)
        return result

    wrapped._production_fix = True
    bot.generate_quant_signal = wrapped
    _GENERATION_PATCHED = True


def _keyboard_patch(bot: Any) -> None:
    original = getattr(bot, "get_main_keyboard", None)
    if original is None or getattr(original, "_production_fix", False):
        return

    def keyboard():
        try:
            from telegram import KeyboardButton, ReplyKeyboardMarkup
            base = original()
            rows = [list(row) for row in (getattr(base, "keyboard", None) or [])]
            labels = {str(cell.text) for row in rows for cell in row if hasattr(cell, "text")}
            if "🧑‍⚖️ محامي الصفقة" not in labels or "📰 أخبار الذهب" not in labels:
                rows.append([KeyboardButton("🧑‍⚖️ محامي الصفقة"), KeyboardButton("📰 أخبار الذهب")])
            return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
        except Exception:
            return original()

    keyboard._production_fix = True
    bot.get_main_keyboard = keyboard


def _stats_message(bot: Any) -> str:
    conn = None
    try:
        conn = bot.get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM runtime_decision_events")
        total = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT direction, COUNT(*) FROM runtime_decision_events GROUP BY direction")
        candidates = dict(cur.fetchall())
        cur.execute("SELECT direction, COUNT(*) FROM runtime_decision_events WHERE final_decision IN ('APPROVE','APPROVE_WITH_CAUTION') GROUP BY direction")
        approved = dict(cur.fetchall())
        cur.execute("SELECT COUNT(*) FROM trades WHERE outcome IS NULL AND trade_status IN ('OPEN','TP1_HIT')")
        active = int(cur.fetchone()[0] or 0)
        return ("📊 تقرير القرار\n"
                f"المرشحون المسجلون: {total}\n"
                f"BUY: {candidates.get('BUY',0)} مرشح / {approved.get('BUY',0)} مقبول\n"
                f"SELL: {candidates.get('SELL',0)} مرشح / {approved.get('SELL',0)} مقبول\n"
                f"الصفقات النشطة: {active}\n\n"
                "الإحصائيات مبنية على سجل القرارات النهائي، وليس على عدد الرسائل المرسلة فقط.")
    except Exception as exc:
        return f"⚠️ تعذر قراءة تقرير القرار حالياً: {type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try:
                bot.release_db_connection(conn)
            except Exception:
                pass


def _install_application_router(bot: Any) -> None:
    global _APPLICATION_PATCHED
    try:
        from telegram.ext import Application
    except Exception as exc:
        LOGGER.warning("Telegram Application unavailable: %s", exc)
        return
    with _LOCK:
        current = getattr(Application, "process_update", None)
        if current is None or getattr(current, "_production_fix", False):
            _APPLICATION_PATCHED = current is not None
            return

        async def process_update(app, update):
            try:
                message = getattr(update, "message", None)
                text = str(getattr(message, "text", "") or "").strip()
                if text in {"🧑‍⚖️ محامي الصفقة", "/lawyer"}:
                    from decision_layer import _lawyer_handler
                    await _lawyer_handler(update, type("Ctx", (), {"application": app})())
                    return
                if text in {"📰 أخبار الذهب", "/news"}:
                    from decision_layer import _news_handler
                    await _news_handler(update, type("Ctx", (), {"application": app})())
                    return
                if text in {"📈 إحصائيات النظام", "/stats"}:
                    await message.reply_text(_stats_message(bot), reply_markup=bot.get_main_keyboard())
                    return
            except Exception as exc:
                LOGGER.exception("Telegram canonical routing failed: %s", exc)
            await current(app, update)

        process_update._production_fix = True
        Application.process_update = process_update
        _APPLICATION_PATCHED = True


def install(bot: Any) -> None:
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        _INSTALLED = True
    try:
        _ensure_schema(bot)
        _keyboard_patch(bot)
        _patch_generation(bot)
        _install_application_router(bot)
        cache = getattr(bot, "GLOBAL_CACHE", None)
        if isinstance(cache, dict):
            cache["production_fix"] = {"installed": True, "installed_at": datetime.now(timezone.utc).isoformat(), "generation_patch": _GENERATION_PATCHED, "telegram_router": _APPLICATION_PATCHED}
        LOGGER.info("✅ Final production hardening installed: generation=%s telegram=%s", _GENERATION_PATCHED, _APPLICATION_PATCHED)
    except Exception as exc:
        LOGGER.exception("❌ Final production hardening failed: %s", exc)
        with _LOCK:
            _INSTALLED = False


def health() -> dict[str, Any]:
    with _LOCK:
        return {"installed": _INSTALLED, "generation_patch": _GENERATION_PATCHED, "telegram_router": _APPLICATION_PATCHED}

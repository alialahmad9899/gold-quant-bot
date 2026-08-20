"""Runtime bridge between the existing bot signal loop and Phase 2 lifecycle state."""
from __future__ import annotations

import threading
import time
from typing import Any

from trade_intelligence import TradeIntelligence, TradeThesis
from trade_state_store import TradeStateStore


class Phase2RuntimeIntegration:
    def __init__(self, bot_module: Any, manager: TradeIntelligence | None = None):
        self.bot = bot_module
        self.manager = manager or TradeIntelligence(TradeStateStore())
        self._installed = False
        self._original_generate = None
        self._original_monitor = None
        self._original_has_active = None

    @staticmethod
    def _direction(value: Any) -> str | None:
        raw = str(value or "").upper()
        if "BUY" in raw or "شراء" in raw:
            return "BUY"
        if "SELL" in raw or "بيع" in raw:
            return "SELL"
        return None

    def build_trade_thesis(self, candidate_signal: dict, market_summary: dict | None = None, smc: dict | None = None) -> TradeThesis:
        market_summary = market_summary or {}
        smc = smc or {}
        direction = self._direction(candidate_signal.get("type")) or self._direction(candidate_signal.get("direction"))
        structure = {
            "smc": dict(smc),
            "smc_note": candidate_signal.get("smc_note", ""),
            "score_bull": candidate_signal.get("score_bull"),
            "score_bear": candidate_signal.get("score_bear"),
            "signal_candle_close": candidate_signal.get("signal_candle_close"),
            "signal_candle_time": candidate_signal.get("signal_candle_time", ""),
            "h4_trend": market_summary.get("h4_trend", ""),
            "state_label": market_summary.get("state_label", ""),
        }
        reason = candidate_signal.get("smc_note") or "تم قبول الإشارة بعد اجتياز محرك التحليل الحالي"
        return TradeThesis(
            direction=direction or "UNKNOWN",
            entry=float(candidate_signal.get("entry") or 0.0),
            sl=float(candidate_signal["sl"]) if candidate_signal.get("sl") is not None else None,
            tp1=float(candidate_signal["tp1"]) if candidate_signal.get("tp1") is not None else None,
            tp2=float(candidate_signal["tp2"]) if candidate_signal.get("tp2") is not None else None,
            h4_trend=str(market_summary.get("h4_trend") or ""),
            bos=bool(smc.get("bos") or smc.get("bos_bullish") or smc.get("bos_bearish")),
            fvg=bool(smc.get("fvg_bullish") or smc.get("fvg_bearish")),
            liquidity=bool(
                smc.get("liquidity")
                or smc.get("sweep_bullish")
                or smc.get("sweep_bearish")
            ),
            gemini_decision=str(candidate_signal.get("gemini_note") or ""),
            confidence=float(candidate_signal.get("confidence") or 0.0) / 100.0,
            notes=[reason],
            structure=structure,
            candle_id=str(candidate_signal.get("candle_id") or ""),
            reason=reason,
        )

    def _ledger_has_open_trade(self) -> bool:
        if not self._original_has_active:
            return False
        return bool(self._original_has_active("BUY") or self._original_has_active("SELL"))

    def _reconcile_ledger(self) -> None:
        if self.manager.has_active_trade() and not self._ledger_has_open_trade():
            self.manager.close("تمت مزامنة حالة Phase 2 مع دفتر الصفقات ولا توجد صفقة مفتوحة")

    def _review_flags(self) -> dict:
        trade = self.manager.active_trade
        if not trade:
            return {}
        market = self.bot.get_market_data() if hasattr(self.bot, "get_market_data") else {}
        feed = dict(market.get("price_feed") or {})
        price = feed.get("mid") or feed.get("spot")
        flags: dict[str, Any] = {}

        try:
            price_f = float(price) if price is not None else None
            entry = float(trade.thesis.entry)
            sl = float(trade.thesis.sl) if trade.thesis.sl is not None else None
            tp1 = float(trade.thesis.tp1) if trade.thesis.tp1 is not None else None
            risk = abs(entry - sl) if sl is not None else 0.0
            if price_f is not None and tp1 is not None:
                flags["tp1_reached"] = price_f >= tp1 if trade.thesis.direction == "BUY" else price_f <= tp1
            if price_f is not None and risk > 0 and not flags.get("tp1_reached"):
                adverse = (entry - price_f) if trade.thesis.direction == "BUY" else (price_f - entry)
                flags["temporary_pressure"] = adverse >= risk * 0.50
        except (TypeError, ValueError):
            pass

        analyzer = getattr(self.bot, "analyze_institutional_engine", None)
        if analyzer is not None:
            try:
                analysis = analyzer() or {}
                h4_trend = str(analysis.get("h4_trend") or "")
                state = str(analysis.get("state_label") or "")
                smc = dict(analysis.get("smc") or {})
                opposite_h4 = (
                    trade.thesis.direction == "BUY" and h4_trend == "BEARISH"
                ) or (
                    trade.thesis.direction == "SELL" and h4_trend == "BULLISH"
                )
                opposite_state = (
                    trade.thesis.direction == "BUY" and state == "BEARISH"
                ) or (
                    trade.thesis.direction == "SELL" and state == "BULLISH"
                )
                opposite_smc = (
                    (trade.thesis.direction == "BUY" and (smc.get("fvg_bearish") or smc.get("sweep_bearish")))
                    or (trade.thesis.direction == "SELL" and (smc.get("fvg_bullish") or smc.get("sweep_bullish")))
                )
                flags["structure_break_against"] = bool(opposite_h4 and opposite_smc)
                flags["invalidation"] = bool(opposite_h4 and opposite_state and opposite_smc)
                flags["reversal_signal"] = bool(opposite_h4 and opposite_state and opposite_smc)
            except Exception:
                pass
        return flags

    def review_active_trade(self) -> dict:
        if not self.manager.has_active_trade():
            return {"decision": "KEEP", "state": "NO_POSITION", "management": {}}
        result = self.manager.manage(self._review_flags())
        cache = getattr(self.bot, "GLOBAL_CACHE", None)
        if isinstance(cache, dict):
            cache["active_trade_management"] = result
        return result

    def _wrapped_has_active(self, signal_type: str) -> bool:
        if self.manager.has_active_trade():
            return True
        return bool(self._original_has_active(signal_type)) if self._original_has_active else False

    def _wrapped_generate(self, *args, **kwargs):
        if self.manager.has_active_trade():
            review = self.review_active_trade()
            return {
                "status": "WAIT",
                "reason": f"حماية Phase 2: توجد صفقة نشطة وحالتها {review['state']}؛ تم منع فتح صفقة موازية حتى حسمها.",
                "price": self.bot.get_market_data().get("gold", 0.0),
                "phase2": review,
            }

        result = self._original_generate(*args, **kwargs)
        if not isinstance(result, dict) or result.get("status") != "SIGNAL":
            return result

        market_summary = {
            "h4_trend": result.get("h4_trend", ""),
            "state_label": result.get("state_label", ""),
        }
        smc = result.get("smc") or {}
        analyzer = getattr(self.bot, "analyze_institutional_engine", None)
        if analyzer is not None and (not market_summary["h4_trend"] or not smc):
            try:
                analysis = analyzer() or {}
                market_summary["h4_trend"] = analysis.get("h4_trend", market_summary["h4_trend"])
                market_summary["state_label"] = analysis.get("state_label", market_summary["state_label"])
                smc = dict(analysis.get("smc") or smc)
            except Exception:
                pass

        thesis = self.build_trade_thesis(result, market_summary, smc)
        if not self.manager.open_trade(thesis):
            return {
                "status": "WAIT",
                "reason": "تم منع الإشارة بسبب قفل الصفقة النشطة في Phase 2.",
                "price": result.get("entry", 0.0),
            }
        result["phase2_state"] = "ACTIVE"
        result["phase2_thesis_logged"] = True
        return result

    def _wrapped_monitor(self, *args, **kwargs):
        result = self._original_monitor(*args, **kwargs)
        if self.manager.has_active_trade():
            self.review_active_trade()
            self._reconcile_ledger()
        return result

    def install(self) -> "Phase2RuntimeIntegration":
        if self._installed:
            return self
        self._original_has_active = getattr(self.bot, "has_active_open_trade", None)
        self._original_generate = getattr(self.bot, "generate_quant_signal", None)
        self._original_monitor = getattr(self.bot, "monitor_open_trades", None)
        if self._original_has_active is None or self._original_generate is None:
            raise AttributeError("Phase 2 integration requires signal pipeline functions")
        self.bot.has_active_open_trade = self._wrapped_has_active
        self.bot.generate_quant_signal = self._wrapped_generate
        if self._original_monitor is not None:
            self.bot.monitor_open_trades = self._wrapped_monitor
        self._reconcile_ledger()
        self._installed = True
        return self


def install_phase2_when_bot_ready(timeout_seconds: float = 60.0) -> Phase2RuntimeIntegration | None:
    import sys

    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        bot = sys.modules.get("bot") or sys.modules.get("__main__")
        if bot is not None and hasattr(bot, "generate_quant_signal"):
            return Phase2RuntimeIntegration(bot).install()
        time.sleep(0.25)
    return None


def start_phase2_runtime_bootstrap() -> threading.Thread:
    thread = threading.Thread(
        target=install_phase2_when_bot_ready,
        name="phase2-trade-intelligence-bootstrap",
        daemon=True,
    )
    thread.start()
    return thread

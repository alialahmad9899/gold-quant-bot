"""Institutional-style pre-trade risk review for XAU/USD.

The engine is deterministic and fail-closed for hard risk violations. It does
not replace SMC/HMM/ML/Gemini; it evaluates their output before a Phase 2
trade becomes active.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Any

MIN_RR = float(os.getenv("INSTITUTIONAL_MIN_RR", "1.50"))
MIN_CONFIDENCE = float(os.getenv("INSTITUTIONAL_MIN_CONFIDENCE", "45"))
APPROVE_SCORE = int(os.getenv("INSTITUTIONAL_APPROVE_SCORE", "72"))
MODIFY_SCORE = int(os.getenv("INSTITUTIONAL_MODIFY_SCORE", "58"))


@dataclass
class TradeReviewResult:
    approved: bool
    decision: str
    risk_score: int
    reason: str
    thesis: str
    invalidation: str
    reversal: str
    regime: str
    hard_vetoes: list[str]
    matched_lessons: list[str]
    counter_trade_risk: str
    component_scores: dict[str, int]
    ai_review: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _direction(value: Any) -> str | None:
    raw = str(value or "").upper()
    if "BUY" in raw or "شراء" in raw:
        return "BUY"
    if "SELL" in raw or "بيع" in raw:
        return "SELL"
    return None


def _float(value: Any) -> float | None:
    try:
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"true", "1", "yes", "bullish", "bearish"}


def _rr(direction: str, entry: float | None, sl: float | None, tp1: float | None) -> float | None:
    if not all(v is not None for v in (entry, sl, tp1)):
        return None
    risk = abs(entry - sl)
    reward = (tp1 - entry) if direction == "BUY" else (entry - tp1)
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def infer_regime(market_summary: dict[str, Any]) -> str:
    h4 = str(market_summary.get("h4_trend") or "").upper()
    state = str(market_summary.get("state_label") or "").upper()
    volatility = str(market_summary.get("volatility_regime") or "").upper()
    if "TRANSITION" in state or "TRANSITION" in h4:
        return "TRANSITION"
    if "HIGH" in volatility:
        return "HIGH_VOLATILITY"
    if "LOW" in volatility:
        return "LOW_VOLATILITY"
    if h4 == "BULLISH" and state == "BULLISH":
        return "TRENDING_BULLISH"
    if h4 == "BEARISH" and state == "BEARISH":
        return "TRENDING_BEARISH"
    if "RANG" in state:
        return "RANGING"
    if h4 in {"BULLISH", "BEARISH"}:
        return "TRANSITION"
    return "UNKNOWN"


def _smc_alignment(direction: str, smc: dict[str, Any], note: str) -> tuple[int, int]:
    text = str(note or "").upper()
    bull = any(_bool(smc.get(k)) for k in ("bos_bullish", "fvg_bullish", "sweep_bullish", "liquidity_bullish"))
    bear = any(_bool(smc.get(k)) for k in ("bos_bearish", "fvg_bearish", "sweep_bearish", "liquidity_bearish"))
    bull = bull or any(x in text for x in ("BULLISH", "شراء", "صاعد", "سيولة صاعدة", "FVG صاعد"))
    bear = bear or any(x in text for x in ("BEARISH", "SELL", "بيع", "هابط", "سيولة هابطة", "FVG هابط"))
    supporting = bull if direction == "BUY" else bear
    opposing = bear if direction == "BUY" else bull
    return int(supporting), int(opposing)


def _lesson_severity(text: str) -> str:
    raw = str(text or "").lower()
    if any(k in raw for k in ("critical", "حرج", "veto", "لا تدخل", "ممنوع", "حظر")):
        return "HIGH"
    if any(k in raw for k in ("high", "خطير", "تحذير", "خطر")):
        return "HIGH"
    if any(k in raw for k in ("medium", "متوسط")):
        return "MEDIUM"
    return "LOW"


def _lesson_applies(direction: str, lesson: str, context: str) -> bool:
    raw = str(lesson or "")
    low = raw.lower()
    if direction == "BUY" and any(k in low for k in ("sell", "بيع", "هبوط", "هابط")) and not any(k in low for k in ("buy", "شراء", "صعود", "صاعد")):
        return False
    if direction == "SELL" and any(k in low for k in ("buy", "شراء", "صعود", "صاعد")) and not any(k in low for k in ("sell", "بيع", "هبوط", "هابط")):
        return False
    words = [w for w in re.findall(r"[\w\u0600-\u06ff]+", low) if len(w) >= 4]
    context_low = context.lower()
    signal_words = [w for w in words if w in context_low]
    return len(signal_words) >= 1 or (_lesson_severity(raw) == "HIGH" and len(words) <= 4)


def _historical_lesson_score(direction: str, lessons: list[Any], context: str) -> tuple[int, list[str], list[str]]:
    score = 5
    matched: list[str] = []
    vetoes: list[str] = []
    for raw in lessons or []:
        lesson = str(raw or "").strip()
        if not lesson or not _lesson_applies(direction, lesson, context):
            continue
        matched.append(lesson)
        severity = _lesson_severity(lesson)
        if severity == "HIGH":
            score = 0
            vetoes.append(f"درس عالي الخطورة من الصفقات السابقة: {lesson}")
        elif severity == "MEDIUM":
            score = max(1, score - 2)
        else:
            score = max(2, score - 1)
    return score, matched, vetoes


def review_trade(signal_data: dict[str, Any], market_summary: dict[str, Any], lessons: list[Any] | None = None, smc: dict[str, Any] | None = None) -> TradeReviewResult:
    signal_data = dict(signal_data or {})
    market_summary = dict(market_summary or {})
    smc = dict(smc or signal_data.get("smc") or {})

    direction = _direction(signal_data.get("type")) or _direction(signal_data.get("direction"))
    entry = _float(signal_data.get("entry"))
    sl = _float(signal_data.get("sl"))
    tp1 = _float(signal_data.get("tp1"))
    confidence = _float(signal_data.get("confidence")) or 0.0
    rsi = _float(signal_data.get("rsi"))
    dxy_corr = _float(signal_data.get("dxy_corr"))
    note = str(signal_data.get("smc_note") or "")
    h4 = str(market_summary.get("h4_trend") or "").upper()
    state = str(market_summary.get("state_label") or "").upper()
    regime = infer_regime(market_summary)

    hard_vetoes: list[str] = []
    counter_risk = "منخفض"
    if direction is None:
        hard_vetoes.append("تعذر تحديد اتجاه الصفقة.")
    if entry is None or entry <= 0:
        hard_vetoes.append("سعر الدخول غير صالح.")
    if sl is None or tp1 is None:
        hard_vetoes.append("يجب أن يكون وقف الخسارة والهدف الأول محددين.")

    rr = _rr(direction or "BUY", entry, sl, tp1)
    if rr is None:
        hard_vetoes.append("هيكل SL/TP غير صالح للاتجاه المقترح.")
    elif rr < MIN_RR:
        hard_vetoes.append(f"العائد إلى المخاطرة {rr:.2f} أقل من الحد المؤسسي {MIN_RR:.2f}.")

    if confidence < MIN_CONFIDENCE:
        hard_vetoes.append(f"الثقة الإحصائية {confidence:.1f}% أقل من الحد الأدنى {MIN_CONFIDENCE:.1f}%.")

    if direction == "BUY" and h4 == "BEARISH":
        hard_vetoes.append("BUY يتعارض مع اتجاه H4 الهابط.")
    if direction == "SELL" and h4 == "BULLISH":
        hard_vetoes.append("SELL يتعارض مع اتجاه H4 الصاعد.")

    if direction == "BUY" and state in {"BEARISH", "STRONG_BEARISH"}:
        hard_vetoes.append("حالة HMM هابطة ضد BUY.")
    if direction == "SELL" and state in {"BULLISH", "STRONG_BULLISH"}:
        hard_vetoes.append("حالة HMM صاعدة ضد SELL.")

    if direction == "BUY" and sl is not None and entry is not None and sl >= entry:
        hard_vetoes.append("وقف BUY يجب أن يكون أسفل الدخول.")
    if direction == "SELL" and sl is not None and entry is not None and sl <= entry:
        hard_vetoes.append("وقف SELL يجب أن يكون أعلى الدخول.")
    if direction == "BUY" and tp1 is not None and entry is not None and tp1 <= entry:
        hard_vetoes.append("TP1 في BUY يجب أن يكون أعلى الدخول.")
    if direction == "SELL" and tp1 is not None and entry is not None and tp1 >= entry:
        hard_vetoes.append("TP1 في SELL يجب أن يكون أسفل الدخول.")

    if rsi is not None:
        if direction == "BUY" and rsi >= 78:
            hard_vetoes.append(f"RSI مرتفع جداً ({rsi:.1f}) ويشير إلى دخول متأخر/إجهاد سعري.")
        if direction == "SELL" and rsi <= 22:
            hard_vetoes.append(f"RSI منخفض جداً ({rsi:.1f}) ويشير إلى دخول متأخر/إجهاد سعري.")

    supporting_smc, opposing_smc = _smc_alignment(direction or "BUY", smc, note)
    opposite_h4 = (direction == "BUY" and h4 == "BEARISH") or (direction == "SELL" and h4 == "BULLISH")
    opposite_state = (direction == "BUY" and state in {"BEARISH", "STRONG_BEARISH"}) or (direction == "SELL" and state in {"BULLISH", "STRONG_BULLISH"})
    if opposite_h4 and (opposite_state or opposing_smc):
        counter_risk = "مرتفع"
        hard_vetoes.append("اختبار الاتجاه المعاكس أقوى من thesis الحالية.")
    elif opposite_h4 or opposite_state or opposing_smc:
        counter_risk = "متوسط"

    if direction == "BUY" and dxy_corr is not None and dxy_corr > 0.45:
        hard_vetoes.append(f"ارتباط DXY موجب بقوة ({dxy_corr:.2f}) ضد BUY على الذهب.")
    if direction == "SELL" and dxy_corr is not None and dxy_corr < -0.45:
        hard_vetoes.append(f"ارتباط DXY سالب بقوة ({dxy_corr:.2f}) ضد SELL على الذهب.")

    context = json.dumps({"signal": signal_data, "market": market_summary}, ensure_ascii=False, default=str)
    historical_score, matched_lessons, lesson_vetoes = _historical_lesson_score(direction or "BUY", lessons or [], context)
    hard_vetoes.extend(lesson_vetoes)

    structure_score = 20 if supporting_smc else 8
    trend_score = 20 if ((direction == "BUY" and h4 == "BULLISH") or (direction == "SELL" and h4 == "BEARISH")) else 7
    if state in {"RANGING", "TRANSITION", ""}:
        trend_score = min(trend_score, 12)
    entry_score = 15 if rr is not None and rr >= 2.0 else 11 if rr is not None else 0
    rr_score = min(15, max(0, int((rr or 0) / 3.0 * 15)))
    momentum_score = 10
    if rsi is not None:
        if direction == "BUY":
            momentum_score = 8 if rsi < 70 else 5 if rsi < 76 else 2
        else:
            momentum_score = 8 if rsi > 30 else 5 if rsi > 24 else 2
    liquidity_score = 10 if supporting_smc else 5 if opposing_smc else 2
    regime_score = 5 if regime in {"TRENDING_BULLISH", "TRENDING_BEARISH"} and ((direction == "BUY" and h4 == "BULLISH") or (direction == "SELL" and h4 == "BEARISH")) else 2

    component_scores = {
        "structure": structure_score,
        "trend_alignment": trend_score,
        "entry_quality": entry_score,
        "risk_reward": rr_score,
        "momentum": momentum_score,
        "liquidity": liquidity_score,
        "regime_fit": regime_score,
        "historical_risk": historical_score,
    }
    total = max(0, min(100, sum(component_scores.values())))

    if hard_vetoes:
        decision = "REJECT"
        approved = False
        reason = "فيتو مخاطر مؤسسي: " + " | ".join(hard_vetoes[:3])
    elif total >= APPROVE_SCORE:
        decision = "APPROVE"
        approved = True
        reason = f"اجتازت الصفقة بوابة المخاطر بدرجة {total}/100 دون سبب فيتو جوهري."
    elif total >= MODIFY_SCORE:
        decision = "MODIFY"
        approved = False
        reason = f"الفكرة مقبولة جزئياً لكن جودة الصفقة {total}/100 وتحتاج تحسين الدخول/SL/TP."
    else:
        decision = "REJECT"
        approved = False
        reason = f"جودة الصفقة منخفضة ({total}/100) ولا تبرر المخاطرة الحالية."

    thesis = (
        f"{direction or 'UNKNOWN'} مبنية على توافق H4={h4 or 'غير معروف'}، HMM={state or 'غير معروف'}، "
        f"وتأكيد SMC={'موجود' if supporting_smc else 'ضعيف'} مع RR={rr:.2f}"
        if rr is not None else
        f"{direction or 'UNKNOWN'} بدون بنية مخاطرة مكتملة بما يكفي لاعتماد thesis."
    )
    invalidation = "كسر مستوى SL أو كسر آخر بنية M15 في الاتجاه المعاكس مع تحول HMM/H4 ضد الصفقة."
    reversal = "BOS/CHoCH معاكس + تحول H4/HMM للاتجاه المقابل + تأكيد سيولة/FVG معاكسة."

    return TradeReviewResult(
        approved=approved,
        decision=decision,
        risk_score=total,
        reason=reason,
        thesis=thesis,
        invalidation=invalidation,
        reversal=reversal,
        regime=regime,
        hard_vetoes=hard_vetoes,
        matched_lessons=matched_lessons,
        counter_trade_risk=counter_risk,
        component_scores=component_scores,
    )


def build_adversarial_prompt(signal_data: dict[str, Any], market_summary: dict[str, Any], deterministic: TradeReviewResult, lessons: list[Any]) -> str:
    payload = json.dumps({"signal": signal_data, "market": market_summary}, ensure_ascii=False, default=str)
    lesson_text = "\n".join(f"- {x}" for x in lessons or []) or "- لا توجد دروس تاريخية متاحة"
    return f"""
أنت تعمل كمدير مخاطر كمي مؤسسي متخصص في XAU/USD. لا تفترض صحة الإشارة لمجرد أنها صادرة عن المحرك الكمي.

مهمتك adversarial review: افترض أن الصفقة ستخسر، وابحث عن أقوى سبب لفشلها قبل الموافقة.

افحص تحديداً:
1) تضارب H4/HMM/SMC.
2) الدخول المتأخر أو قرب منطقة invalidation.
3) RR غير واقعي أو TP غير منطقي.
4) liquidity sweep / fake breakout / exhaustion.
5) السيناريو المعاكس للصفقة وهل هو أقوى.
6) الدروس التاريخية وتطبيقها الفعلي على الصفقة.
7) هل thesis واضحة وهل يمكن تحديد invalidation وreversal.

التقييم الحتمي المسبق (لا يجوز تجاهله أو تجاوزه إذا فيه hard veto):
{json.dumps(deterministic.to_dict(), ensure_ascii=False, default=str)}

الدروس السابقة:
{lesson_text}

بيانات الصفقة:
{payload}

إذا كانت هناك مخالفة جوهرية، يجب أن يكون approved=false. لا تخترع أرقاماً غير موجودة.
أرجع JSON فقط:
{{
  "approved": true,
  "decision": "APPROVE",
  "reason": "سبب عربي مختصر",
  "thesis": "ملخص الفكرة",
  "invalidation": "شرط بطلان الفكرة",
  "reversal": "شرط الانعكاس"
}}
""".strip()


def parse_ai_review(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {"approved": False, "decision": "INVALID", "reason": "تعذر قراءة مراجعة Gemini المؤسسية."}
    return value if isinstance(value, dict) else {"approved": False, "decision": "INVALID", "reason": "استجابة Gemini غير صالحة."}


def apply_ai_review(deterministic: TradeReviewResult, raw_ai: Any) -> TradeReviewResult:
    ai = parse_ai_review(raw_ai)
    ai_approved = _bool(ai.get("approved"))
    ai_decision = str(ai.get("decision") or "").upper()
    ai_reason = str(ai.get("reason") or "").strip()
    ai_result = dict(ai)
    ai_result["approved"] = ai_approved
    ai_result["decision"] = ai_decision or ("APPROVE" if ai_approved else "REJECT")

    deterministic.ai_review = ai_result
    if deterministic.hard_vetoes:
        return deterministic
    if not ai_approved or ai_decision in {"REJECT", "MODIFY", "REVERSE"}:
        deterministic.approved = False
        deterministic.decision = "REJECT"
        deterministic.reason = ai_reason or "مراجعة Gemini adversarial رفضت الفكرة لأسباب تتعلق بالمخاطر."
        return deterministic
    if deterministic.decision != "APPROVE":
        deterministic.approved = False
        return deterministic
    deterministic.reason = ai_reason or deterministic.reason
    if ai.get("thesis"):
        deterministic.thesis = str(ai["thesis"])
    if ai.get("invalidation"):
        deterministic.invalidation = str(ai["invalidation"])
    if ai.get("reversal"):
        deterministic.reversal = str(ai["reversal"])
    return deterministic

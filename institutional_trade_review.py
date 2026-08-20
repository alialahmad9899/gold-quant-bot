"""Flexible institutional-style pre-trade review for XAU/USD.

The deterministic engine owns hard risk vetoes. RR, confidence, regime mismatch,
and Gemini disagreement are normally quality/advisory signals rather than hard
blocks. Gemini may be promoted to a hard veto only with INSTITUTIONAL_AI_VETO=1.
"""
from __future__ import annotations
import json, os, re
from dataclasses import asdict, dataclass
from typing import Any

MIN_RR = float(os.getenv("INSTITUTIONAL_MIN_RR", "1.20")); MIN_CONFIDENCE = float(os.getenv("INSTITUTIONAL_MIN_CONFIDENCE", "40")); APPROVE_SCORE = int(os.getenv("INSTITUTIONAL_APPROVE_SCORE", "60")); MODIFY_SCORE = int(os.getenv("INSTITUTIONAL_MODIFY_SCORE", "48")); AI_VETO_ENABLED = os.getenv("INSTITUTIONAL_AI_VETO", "0") == "1"

@dataclass
class TradeReviewResult:
    approved: bool; decision: str; risk_score: int; reason: str; thesis: str; invalidation: str; reversal: str; regime: str; hard_vetoes: list[str]; matched_lessons: list[str]; counter_trade_risk: str; component_scores: dict[str, int]; ai_review: dict[str, Any] | None = None
    def to_dict(self) -> dict[str, Any]: return asdict(self)

def _direction(value: Any) -> str | None:
    raw = str(value or "").upper(); return "BUY" if "BUY" in raw or "شراء" in raw else "SELL" if "SELL" in raw or "بيع" in raw else None

def _float(value: Any) -> float | None:
    try:
        value = float(value); return None if value != value or value in (float("inf"), float("-inf")) else value
    except (TypeError, ValueError): return None

def _bool(value: Any) -> bool:
    if isinstance(value, bool): return value
    if isinstance(value, (int, float)): return bool(value)
    return str(value or "").strip().lower() in {"true", "1", "yes", "bullish", "bearish"}

def _rr(direction: str, entry: float | None, sl: float | None, tp1: float | None) -> float | None:
    if not all(v is not None for v in (entry, sl, tp1)): return None
    risk = abs(entry - sl); reward = (tp1 - entry) if direction == "BUY" else (entry - tp1); return reward / risk if risk > 0 and reward > 0 else None

def infer_regime(market_summary: dict[str, Any]) -> str:
    h4 = str(market_summary.get("h4_trend") or "").upper(); state = str(market_summary.get("state_label") or "").upper(); vol = str(market_summary.get("volatility_regime") or "").upper()
    if "TRANSITION" in state or "TRANSITION" in h4: return "TRANSITION"
    if "HIGH" in vol: return "HIGH_VOLATILITY"
    if "LOW" in vol: return "LOW_VOLATILITY"
    if h4 == "BULLISH" and state == "BULLISH": return "TRENDING_BULLISH"
    if h4 == "BEARISH" and state == "BEARISH": return "TRENDING_BEARISH"
    if "RANG" in state: return "RANGING"
    if h4 in {"BULLISH", "BEARISH"}: return "TRANSITION"
    return "UNKNOWN"

def _smc_alignment(direction: str, smc: dict[str, Any], note: str) -> tuple[int, int]:
    text = str(note or "").upper(); bull = any(_bool(smc.get(k)) for k in ("bos_bullish", "fvg_bullish", "sweep_bullish", "liquidity_bullish")) or any(x in text for x in ("BULLISH", "شراء", "صاعد")); bear = any(_bool(smc.get(k)) for k in ("bos_bearish", "fvg_bearish", "sweep_bearish", "liquidity_bearish")) or any(x in text for x in ("BEARISH", "SELL", "بيع", "هابط")); return int(bull if direction == "BUY" else bear), int(bear if direction == "BUY" else bull)

def _lesson_severity(text: str) -> str:
    low = str(text or "").lower()
    if any(k in low for k in ("critical", "حرج", "veto", "لا تدخل", "ممنوع", "حظر", "high", "خطير", "خطر")): return "HIGH"
    if any(k in low for k in ("medium", "متوسط")): return "MEDIUM"
    return "LOW"

def _lesson_applies(direction: str, lesson: str, context: str) -> bool:
    low = str(lesson or "").lower()
    if direction == "BUY" and any(k in low for k in ("sell", "بيع", "هبوط", "هابط")) and not any(k in low for k in ("buy", "شراء", "صعود", "صاعد")): return False
    if direction == "SELL" and any(k in low for k in ("buy", "شراء", "صعود", "صاعد")) and not any(k in low for k in ("sell", "بيع", "هبوط", "هابط")): return False
    words = [w for w in re.findall(r"[\w\u0600-\u06ff]+", low) if len(w) >= 4]; return any(w in context.lower() for w in words) or (_lesson_severity(low) == "HIGH" and len(words) <= 4)

def _historical_lesson_score(direction: str, lessons: list[Any], context: str) -> tuple[int, list[str], list[str]]:
    score = 5; matched: list[str] = []; vetoes: list[str] = []
    for raw in lessons or []:
        lesson = str(raw or "").strip()
        if not lesson or not _lesson_applies(direction, lesson, context): continue
        matched.append(lesson); severity = _lesson_severity(lesson)
        if severity == "HIGH": score = 0; vetoes.append(f"درس عالي الخطورة من الصفقات السابقة: {lesson}")
        elif severity == "MEDIUM": score = max(1, score - 2)
        else: score = max(2, score - 1)
    return score, matched, vetoes

def review_trade(signal_data: dict[str, Any], market_summary: dict[str, Any], lessons: list[Any] | None = None, smc: dict[str, Any] | None = None) -> TradeReviewResult:
    signal_data = dict(signal_data or {}); market_summary = dict(market_summary or {}); smc = dict(smc or signal_data.get("smc") or {}); direction = _direction(signal_data.get("type")) or _direction(signal_data.get("direction")); entry = _float(signal_data.get("entry")); sl = _float(signal_data.get("sl")); tp1 = _float(signal_data.get("tp1")); confidence = _float(signal_data.get("confidence")) or 0.0; rsi = _float(signal_data.get("rsi")); note = str(signal_data.get("smc_note") or ""); h4 = str(market_summary.get("h4_trend") or "").upper(); state = str(market_summary.get("state_label") or "").upper(); regime = infer_regime(market_summary)
    vetoes: list[str] = []; counter_risk = "منخفض"
    if direction is None: vetoes.append("تعذر تحديد اتجاه الصفقة.")
    if entry is None or entry <= 0: vetoes.append("سعر الدخول غير صالح.")
    if sl is None or tp1 is None: vetoes.append("SL وTP1 يجب أن يكونا محددين.")
    rr = _rr(direction or "BUY", entry, sl, tp1)
    if rr is None: vetoes.append("هيكل SL/TP غير صالح للاتجاه المقترح.")
    if direction == "BUY" and sl is not None and entry is not None and sl >= entry: vetoes.append("وقف BUY يجب أن يكون أسفل الدخول.")
    if direction == "SELL" and sl is not None and entry is not None and sl <= entry: vetoes.append("وقف SELL يجب أن يكون أعلى الدخول.")
    if direction == "BUY" and tp1 is not None and entry is not None and tp1 <= entry: vetoes.append("TP1 في BUY يجب أن يكون أعلى الدخول.")
    if direction == "SELL" and tp1 is not None and entry is not None and tp1 >= entry: vetoes.append("TP1 في SELL يجب أن يكون أسفل الدخول.")
    if rsi is not None and ((direction == "BUY" and rsi >= 88) or (direction == "SELL" and rsi <= 12)): vetoes.append(f"RSI متطرف جداً ({rsi:.1f}) ويشير إلى دخول شديد التأخر.")
    supporting_smc, opposing_smc = _smc_alignment(direction or "BUY", smc, note); opposite_h4 = (direction == "BUY" and h4 == "BEARISH") or (direction == "SELL" and h4 == "BULLISH"); opposite_state = (direction == "BUY" and state in {"BEARISH", "STRONG_BEARISH"}) or (direction == "SELL" and state in {"BULLISH", "STRONG_BULLISH"})
    if opposite_h4 and opposite_state and opposing_smc: counter_risk = "مرتفع"; vetoes.append("H4 + HMM + SMC يدعمون الاتجاه المعاكس بقوة.")
    elif opposite_h4 or opposite_state or opposing_smc: counter_risk = "متوسط"
    context = json.dumps({"signal": signal_data, "market": market_summary}, ensure_ascii=False, default=str); historical_score, matched_lessons, lesson_vetoes = _historical_lesson_score(direction or "BUY", lessons or [], context); vetoes.extend(lesson_vetoes)
    structure_score = 20 if supporting_smc else 10; trend_score = 20 if ((direction == "BUY" and h4 == "BULLISH") or (direction == "SELL" and h4 == "BEARISH")) else 9
    if state in {"RANGING", "TRANSITION", ""}: trend_score = min(trend_score, 13)
    # Calibrated scoring: entry quality measures structural entry context, while RR owns RR.
    entry_score = 15 if supporting_smc and rr is not None and rr >= MIN_RR else 11 if supporting_smc else 8
    rr_score = min(15, max(3, int((rr or 0) / 3.0 * 15))) if rr is not None else 0
    confidence_score = min(10, max(2, int(confidence / 10))); confidence_score = max(2, confidence_score - 1) if confidence < MIN_CONFIDENCE else confidence_score
    momentum_score = 10 if rsi is None else (8 if (direction == "BUY" and rsi < 70) or (direction == "SELL" and rsi > 30) else 5)
    if rsi is not None and ((direction == "BUY" and rsi >= 76) or (direction == "SELL" and rsi <= 24)): momentum_score = 3
    liquidity_score = 10 if any(_bool(smc.get(k)) for k in ("liquidity_bullish", "liquidity_bearish", "sweep_bullish", "sweep_bearish")) else 5 if opposing_smc else 3
    regime_score = 5 if regime in {"TRENDING_BULLISH", "TRENDING_BEARISH"} and ((direction == "BUY" and h4 == "BULLISH") or (direction == "SELL" and h4 == "BEARISH")) else 3
    scores = {"structure": structure_score, "trend_alignment": trend_score, "entry_quality": entry_score, "risk_reward": rr_score, "confidence": confidence_score, "momentum": momentum_score, "liquidity": liquidity_score, "regime_fit": regime_score, "historical_risk": historical_score}; total = max(0, min(100, sum(scores.values())))
    if vetoes: decision, approved, reason = "REJECT", False, "فيتو مخاطر جوهري: " + " | ".join(vetoes[:3])
    elif total >= APPROVE_SCORE: decision, approved, reason = "APPROVE", True, f"اجتازت الصفقة بوابة المخاطر بدرجة {total}/100؛ السياسة مرنة ولا تتطلب الكمال."
    elif total >= MODIFY_SCORE: decision, approved, reason = "MODIFY", True, f"الصفقة قابلة للعمل بدرجة {total}/100؛ توجد ملاحظات تحسين لكنها ليست حظراً تلقائياً."
    else: decision, approved, reason = "REJECT", False, f"جودة الصفقة منخفضة نسبياً ({total}/100) ولا يوجد دعم كافٍ للدخول الحالي."
    thesis = f"{direction or 'UNKNOWN'} مع H4={h4 or 'غير معروف'} وHMM={state or 'غير معروف'} وSMC={'مؤيد' if supporting_smc else 'ضعيف'} وRR={(f'{rr:.2f}' if rr is not None else 'غير متاح')}"; invalidation = "كسر SL أو كسر بنية M15 مع تحول HMM/H4 ضد الصفقة."; reversal = "BOS/CHoCH معاكس + تحول H4/HMM + تأكيد سيولة/FVG للاتجاه المقابل."
    return TradeReviewResult(approved, decision, total, reason, thesis, invalidation, reversal, regime, vetoes, matched_lessons, counter_risk, scores)

def build_adversarial_prompt(signal_data: dict[str, Any], market_summary: dict[str, Any], deterministic: TradeReviewResult, lessons: list[Any]) -> str:
    return f"""
أنت مدير مخاطر كمي مرن متخصص في XAU/USD. افترض أن الصفقة قد تفشل وابحث عن الخطر الحقيقي، لكن لا ترفضها لمجرد أنها غير مثالية.
تعامل مع Gemini كمحلل معارض ومستشار، لا كحاجز تداول صارم. لا ترفض إلا إذا وجدت خطراً واضحاً ومادياً.
لا تتجاوز hard vetoes الحتمية. لا تخترع بيانات. أرجع JSON فقط.
التقييم الحتمي: {json.dumps(deterministic.to_dict(), ensure_ascii=False, default=str)}
الدروس: {chr(10).join('- ' + str(x) for x in lessons) if lessons else '- لا توجد دروس'}
الإشارة: {json.dumps(signal_data, ensure_ascii=False, default=str)}
السوق: {json.dumps(market_summary, ensure_ascii=False, default=str)}
{{"approved": true, "decision": "APPROVE", "reason": "سبب عربي مختصر", "thesis": "ملخص", "invalidation": "شرط البطلان", "reversal": "شرط الانعكاس"}}
""".strip()

def parse_ai_review(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict): return raw
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw or "").strip(), flags=re.I | re.S).strip()
    try:
        value = json.loads(text); return value if isinstance(value, dict) else {}
    except (TypeError, ValueError): return {"approved": False, "decision": "INVALID", "reason": "تعذر قراءة مراجعة Gemini المؤسسية."}

def apply_ai_review(deterministic: TradeReviewResult, raw_ai: Any) -> TradeReviewResult:
    ai = parse_ai_review(raw_ai); ai_approved = _bool(ai.get("approved")); ai_decision = str(ai.get("decision") or ("APPROVE" if ai_approved else "REJECT")).upper(); deterministic.ai_review = {**ai, "approved": ai_approved, "decision": ai_decision, "mode": "HARD_VETO" if AI_VETO_ENABLED else "ADVISORY"}
    if deterministic.hard_vetoes: return deterministic
    if not ai_approved or ai_decision in {"REJECT", "REVERSE"}:
        if AI_VETO_ENABLED: deterministic.approved = False; deterministic.decision = "REJECT"; deterministic.reason = str(ai.get("reason") or "مراجعة Gemini رأت خطراً إضافياً.")
        else: deterministic.approved = deterministic.risk_score >= MODIFY_SCORE; deterministic.decision = "APPROVE_WITH_CAUTION" if deterministic.approved else deterministic.decision; deterministic.reason = f"ملاحظة Gemini: {ai.get('reason') or 'تحفظ تحليلي'} — لم تُستخدم كفيتو تلقائي لأن الصفقة اجتازت الحماية الحتمية."
    else:
        if deterministic.risk_score >= MODIFY_SCORE: deterministic.approved = True; deterministic.decision = "APPROVE" if deterministic.risk_score >= APPROVE_SCORE else "APPROVE_WITH_CAUTION"
        deterministic.reason = str(ai.get("reason") or deterministic.reason)
    deterministic.thesis = str(ai.get("thesis") or deterministic.thesis); deterministic.invalidation = str(ai.get("invalidation") or deterministic.invalidation); deterministic.reversal = str(ai.get("reversal") or deterministic.reversal); return deterministic

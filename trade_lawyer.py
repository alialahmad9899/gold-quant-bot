"""Flexible trade-lawyer layer for signal quality and active-position advice.

The pre-trade helper remains compatible with existing callers. The active
position lawyer is advisory: it does not force entries and does not directly
execute orders. Deterministic invalidation/exit logic remains authoritative.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class TradeLawyerDecision:
    decision: str
    score: int
    reasons: list[str]
    warnings: list[str]


def review_trade(signal: dict, context: dict | None = None) -> TradeLawyerDecision:
    """Backward-compatible pre-trade quality review; intentionally permissive."""
    context = context or {}
    score = 50
    reasons: list[str] = []
    warnings: list[str] = []

    entry = float(signal.get("entry", 0) or 0)
    sl = float(signal.get("sl", 0) or 0)
    tp1 = float(signal.get("tp1", 0) or 0)
    tp2 = float(signal.get("tp2", 0) or 0)

    if entry and sl:
        sl_distance = abs(entry - sl)
        if sl_distance > 0:
            reasons.append("SL distance valid")
            score += 5
        else:
            warnings.append("Invalid SL distance")
            score -= 20

    rr = abs(tp1 - entry) / abs(entry - sl) if entry and tp1 and sl and abs(entry - sl) > 0 else float(signal.get("risk_reward", 0) or 0)
    if rr >= 2:
        score += 15
        reasons.append("Risk reward acceptable")
    elif rr < 1:
        score -= 18
        warnings.append("Poor risk reward")

    if tp2 and tp1:
        reasons.append("TP structure validated")

    for key, points, text in [
        ("bos_confirmed", 10, "BOS confirmed"),
        ("choch_confirmed", 8, "CHoCH confirmed"),
        ("fvg_valid", 8, "FVG valid"),
        ("h4_aligned", 10, "H4 trend aligned"),
    ]:
        if signal.get(key):
            score += points
            reasons.append(text)

    if context.get("similar_trade_open"):
        score -= 8
        warnings.append("Similar active trade exists")
    if context.get("near_major_zone"):
        warnings.append("Near important price zone")
        score -= 4
    if context.get("live_quote_valid") is False:
        score -= 30
        warnings.append("Live quote unavailable")
    if context.get("data_quality") not in (None, "OK", "HISTORICAL_HEALTHY"):
        score -= 15
        warnings.append("Data quality warning")

    score = max(0, min(100, score))
    if score < 35:
        decision = "REJECT"
    elif score < 70:
        decision = "MODIFY"
    else:
        decision = "APPROVE"
    return TradeLawyerDecision(decision, score, reasons, warnings)


@dataclass
class ActiveTradeLawyerAdvice:
    action: str
    urgency: str
    confidence: int
    reason: str
    thesis_status: str
    recommended_sl: float | None = None
    recommended_tp1: float | None = None
    recommended_tp2: float | None = None
    add_condition: str = ""
    avoid_condition: str = ""
    ai_used: bool = False
    ai_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


AI_INTERVAL_SECONDS = float(os.getenv("TRADE_LAWYER_AI_INTERVAL_SECONDS", "900"))
ADD_SCORE_THRESHOLD = int(os.getenv("TRADE_LAWYER_MIN_ADD_SCORE", "68"))


def _direction(value: Any) -> str:
    raw = str(value or "").upper()
    if "BUY" in raw or "شراء" in raw:
        return "BUY"
    if "SELL" in raw or "بيع" in raw:
        return "SELL"
    return "UNKNOWN"


def _r_multiple(direction: str, entry: float, price: float, sl: float | None) -> float | None:
    if sl is None:
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    favorable = price - entry if direction == "BUY" else entry - price
    return favorable / risk


def deterministic_active_advice(trade: dict[str, Any], market: dict[str, Any], management: dict[str, Any], review: dict[str, Any] | None = None) -> ActiveTradeLawyerAdvice:
    direction = _direction(trade.get("direction"))
    entry = float(trade.get("entry") or 0.0)
    sl = float(trade["sl"]) if trade.get("sl") is not None else None
    tp1 = float(trade["tp1"]) if trade.get("tp1") is not None else None
    tp2 = float(trade["tp2"]) if trade.get("tp2") is not None else None
    price = float(market.get("price") or entry)
    r = _r_multiple(direction, entry, price, sl)
    state = str(management.get("state") or "ACTIVE")
    lifecycle_decision = str(management.get("decision") or "KEEP")
    risk_score = int((review or {}).get("risk_score") or 0)

    if state == "INVALIDATED" or lifecycle_decision == "REVERSE":
        return ActiveTradeLawyerAdvice(
            "EXIT", "URGENT", 99,
            "الثيسس فقدت صلاحيتها أو ظهرت شروط انعكاس قوية؛ لا تواصل التعزيز، والحسم يجب أن يمر بمحرك المخاطر.",
            "INVALIDATED", avoid_condition="لا تعزز قبل حسم الانعكاس.",
        )
    if state == "TP1_REACHED":
        return ActiveTradeLawyerAdvice(
            "PROTECT_PROFIT", "HIGH", 94,
            "تحقق الهدف الأول؛ الأفضل حماية جزء من الربح ونقل الوقف تدريجياً نحو التعادل/تحت البنية الصالحة، لا الإغلاق الكامل تلقائياً.",
            "INTACT", recommended_sl=entry, recommended_tp1=tp1, recommended_tp2=tp2,
        )
    if state == "UNDER_PRESSURE" or (r is not None and r <= -0.50):
        return ActiveTradeLawyerAdvice(
            "REDUCE_RISK", "HIGH", 88,
            "الصفقة تحت ضغط واضح لكن ذلك لا يعني الخروج الفوري؛ خفّض التعرض أو انتظر تأكيداً جديداً ولا توسع وقف الخسارة لإنقاذها.",
            "STRESSED", add_condition="عودة البنية مع اتجاه الصفقة وتراجع الضغط.", avoid_condition="عدم توسيع SL فقط لتجنب الخسارة.",
        )
    if r is not None and r >= 0.75 and risk_score >= ADD_SCORE_THRESHOLD and state == "ACTIVE":
        return ActiveTradeLawyerAdvice(
            "ADD_ON_CONFIRMATION", "MEDIUM", 72,
            "الصفقة متقدمة والثيسس متماسكة؛ يمكن التفكير بإضافة صغيرة فقط بعد pullback/تأكيد جديد، وليس بمطاردة السعر.",
            "INTACT", add_condition="SMC confirmation جديد + entry أفضل + RR ما زال مناسباً.", avoid_condition="لا تضف بعد اندفاع ممتد.",
        )
    return ActiveTradeLawyerAdvice(
        "HOLD", "LOW", 80,
        "الثيسس ما زالت قابلة للحياة ولا يوجد سبب قوي للخروج أو التعزيز الفوري.",
        "INTACT", recommended_sl=sl, recommended_tp1=tp1, recommended_tp2=tp2,
    )


def build_active_trade_prompt(trade: dict[str, Any], market: dict[str, Any], management: dict[str, Any], review: dict[str, Any] | None, deterministic: ActiveTradeLawyerAdvice) -> str:
    return f"""
أنت «محامي الصفقة» لصفقة XAU/USD مفتوحة بالفعل.
أنت مستشار مرن وليس بوابة صارمة. لا تبحث عن سبب للخروج في كل مراجعة، ولا تعتبر كل تراجع فشلاً.
هدفك حماية رأس المال مع ترك الصفقة تعمل عندما تكون thesis سليمة.

الأفعال المسموحة:
HOLD, PROTECT_PROFIT, REDUCE_RISK, ADD_ON_CONFIRMATION, EXIT, PREPARE_REVERSAL

مبادئ:
- HOLD هو الخيار الطبيعي عندما تكون thesis سليمة ولا يوجد invalidation واضح.
- لا تطلب الخروج بسبب ضوضاء قصيرة الأجل أو تعارض بسيط.
- لا تطلب توسيع SL لإنقاذ الصفقة.
- بعد TP1 يفضّل حماية الربح تدريجياً بدل إغلاق كامل تلقائياً.
- التعزيز توصية مشروطة فقط، ويجب أن تتأكد من pullback/SMC confirmation وعدم مطاردة السعر.
- EXIT/REVERSE فقط عند دليل واضح على invalidation أو تحول بنيوي قوي.
- لا تخترع أي بيانات، وهذه توصية وليست أمراً تنفيذياً.

التقييم الحتمي:
{json.dumps(deterministic.to_dict(), ensure_ascii=False, default=str)}

الصفقة:
{json.dumps(trade, ensure_ascii=False, default=str)}

السوق:
{json.dumps(market, ensure_ascii=False, default=str)}

الإدارة الحالية:
{json.dumps(management, ensure_ascii=False, default=str)}

مراجعة ما قبل الدخول:
{json.dumps(review or {}, ensure_ascii=False, default=str)}

أرجع JSON فقط:
{{
  "action": "HOLD",
  "urgency": "LOW",
  "confidence": 80,
  "reason": "سبب عربي مختصر",
  "thesis_status": "INTACT",
  "recommended_sl": null,
  "recommended_tp1": null,
  "recommended_tp2": null,
  "add_condition": "",
  "avoid_condition": ""
}}
""".strip()


def parse_ai(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.replace("```json", "", 1).replace("```", "").strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def apply_active_ai(deterministic: ActiveTradeLawyerAdvice, raw: Any, model: str = "") -> ActiveTradeLawyerAdvice:
    ai = parse_ai(raw)
    action = str(ai.get("action") or deterministic.action).upper()
    allowed = {"HOLD", "PROTECT_PROFIT", "REDUCE_RISK", "ADD_ON_CONFIRMATION", "EXIT", "PREPARE_REVERSAL"}
    if action not in allowed or deterministic.action == "EXIT":
        return deterministic
    try:
        confidence = int(max(0, min(100, float(ai.get("confidence", deterministic.confidence)))))
    except (TypeError, ValueError):
        confidence = deterministic.confidence
    # AI cannot expand risk autonomously; downgrade aggressive AI additions to a conditional advisory.
    if action == "EXIT" and deterministic.action not in {"REDUCE_RISK", "PROTECT_PROFIT"} and str(ai.get("thesis_status") or "INTACT").upper() == "INTACT":
        action = "HOLD"
    return ActiveTradeLawyerAdvice(
        action=action,
        urgency=str(ai.get("urgency") or deterministic.urgency).upper(),
        confidence=confidence,
        reason=str(ai.get("reason") or deterministic.reason),
        thesis_status=str(ai.get("thesis_status") or deterministic.thesis_status).upper(),
        recommended_sl=ai.get("recommended_sl", deterministic.recommended_sl),
        recommended_tp1=ai.get("recommended_tp1", deterministic.recommended_tp1),
        recommended_tp2=ai.get("recommended_tp2", deterministic.recommended_tp2),
        add_condition=str(ai.get("add_condition") or deterministic.add_condition),
        avoid_condition=str(ai.get("avoid_condition") or deterministic.avoid_condition),
        ai_used=True,
        ai_model=model,
    )


def should_call_active_ai(last_call: float | None, state: str, previous_state: str | None) -> bool:
    if previous_state and previous_state != state:
        return True
    if last_call is None:
        return True
    return (time.monotonic() - last_call) >= AI_INTERVAL_SECONDS

"""Canonical institutional review core."""
from __future__ import annotations
import json, os, re
from dataclasses import asdict, dataclass
from typing import Any
MIN_RR=float(os.getenv("INSTITUTIONAL_MIN_RR","1.20")); MIN_CONFIDENCE=float(os.getenv("INSTITUTIONAL_MIN_CONFIDENCE","40")); APPROVE_SCORE=int(os.getenv("INSTITUTIONAL_APPROVE_SCORE","60")); MODIFY_SCORE=int(os.getenv("INSTITUTIONAL_MODIFY_SCORE","48")); AI_VETO_ENABLED=os.getenv("INSTITUTIONAL_AI_VETO","0")=="1"
@dataclass
class TradeReviewResult:
    approved: bool; decision: str; risk_score: int; reason: str; thesis: str; invalidation: str; reversal: str; regime: str; hard_vetoes: list[str]; matched_lessons: list[str]; counter_trade_risk: str; component_scores: dict[str,int]; ai_review: dict[str,Any]|None=None
    def to_dict(self): return asdict(self)
def _direction(v):
    r=str(v or "").upper(); return "BUY" if "BUY" in r or "شراء" in r else "SELL" if "SELL" in r or "بيع" in r else None
def _float(v):
    try:
        x=float(v); return None if x!=x or x in (float("inf"),float("-inf")) else x
    except (TypeError,ValueError): return None
def _bool(v):
    if isinstance(v,bool): return v
    if isinstance(v,(int,float)): return bool(v)
    return str(v or "").strip().lower() in {"true","1","yes","bullish","bearish"}
def _rr(direction,entry,sl,tp1):
    if not all(v is not None for v in (entry,sl,tp1)): return None
    risk=abs(entry-sl); reward=(tp1-entry) if direction=="BUY" else (entry-tp1); return reward/risk if risk>0 and reward>0 else None
def infer_regime(m):
    h4=str(m.get("h4_trend") or "").upper(); s=str(m.get("state_label") or "").upper(); v=str(m.get("volatility_regime") or "").upper()
    if "TRANSITION" in s or "TRANSITION" in h4: return "TRANSITION"
    if "HIGH" in v: return "HIGH_VOLATILITY"
    if "LOW" in v: return "LOW_VOLATILITY"
    if h4=="BULLISH" and s=="BULLISH": return "TRENDING_BULLISH"
    if h4=="BEARISH" and s=="BEARISH": return "TRENDING_BEARISH"
    if "RANG" in s: return "RANGING"
    if h4 in {"BULLISH","BEARISH"}: return "TRANSITION"
    return "UNKNOWN"
def _smc_alignment(direction,smc,note):
    text=str(note or "").upper(); bull=any(_bool(smc.get(k)) for k in ("bos_bullish","fvg_bullish","sweep_bullish","liquidity_bullish")) or any(x in text for x in ("BULLISH","شراء","صاعد")); bear=any(_bool(smc.get(k)) for k in ("bos_bearish","fvg_bearish","sweep_bearish","liquidity_bearish")) or any(x in text for x in ("BEARISH","SELL","بيع","هابط")); return int(bull if direction=="BUY" else bear),int(bear if direction=="BUY" else bull)
def _lesson_severity(text):
    low=str(text or "").lower()
    if any(k in low for k in ("critical","حرج","veto","لا تدخل","ممنوع","حظر","high","خطير","خطر")): return "HIGH"
    if any(k in low for k in ("medium","متوسط")): return "MEDIUM"
    return "LOW"
def _lesson_applies(direction,lesson,context):
    low=str(lesson or "").lower(); ctx=context.lower(); severity=_lesson_severity(low)
    if direction=="BUY" and any(k in low for k in ("sell","بيع","هبوط","هابط")) and not any(k in low for k in ("buy","شراء","صعود","صاعد")): return False
    if direction=="SELL" and any(k in low for k in ("buy","شراء","صعود","صاعد")) and not any(k in low for k in ("sell","بيع","هبوط","هابط")): return False
    if severity=="HIGH":
        explicit=(direction=="BUY" and any(k in low for k in ("buy","شراء"))) or (direction=="SELL" and any(k in low for k in ("sell","بيع")))
        if explicit:
            keys=[k for k in ("rsi","h4","resistance","support","range","ranging","fvg","bos","sweep","دولار","تشبع") if k in low]
            return not keys or any(k in ctx for k in keys)
        return len(re.findall(r"[\w\u0600-\u06ff]+",low))<=4
    words=[w for w in re.findall(r"[\w\u0600-\u06ff]+",low) if len(w)>=4]; return any(w in ctx for w in words)
def _historical_lesson_score(direction,lessons,context):
    score=5; matched=[]; vetoes=[]
    for raw in lessons or []:
        lesson=str(raw or "").strip()
        if not lesson or not _lesson_applies(direction,lesson,context): continue
        matched.append(lesson); sev=_lesson_severity(lesson)
        if sev=="HIGH": score=0; vetoes.append(f"درس عالي الخطورة من الصفقات السابقة: {lesson}")
        elif sev=="MEDIUM": score=max(1,score-2)
        else: score=max(2,score-1)
    return score,matched,vetoes
def review_trade(signal_data,market_summary,lessons=None,smc=None):
    signal_data=dict(signal_data or {}); market_summary=dict(market_summary or {}); smc=dict(smc or signal_data.get("smc") or {}); direction=_direction(signal_data.get("type")) or _direction(signal_data.get("direction")); entry=_float(signal_data.get("entry")); sl=_float(signal_data.get("sl")); tp1=_float(signal_data.get("tp1")); confidence=_float(signal_data.get("confidence")) or 0.0; rsi=_float(signal_data.get("rsi")); note=str(signal_data.get("smc_note") or ""); h4=str(market_summary.get("h4_trend") or "").upper(); state=str(market_summary.get("state_label") or "").upper(); regime=infer_regime(market_summary); vetoes=[]; counter="منخفض"
    if direction is None: vetoes.append("تعذر تحديد اتجاه الصفقة.")
    if entry is None or entry<=0: vetoes.append("سعر الدخول غير صالح.")
    if sl is None or tp1 is None: vetoes.append("SL وTP1 يجب أن يكونا محددين.")
    rr=_rr(direction or "BUY",entry,sl,tp1)
    if rr is None: vetoes.append("هيكل SL/TP غير صالح للاتجاه المقترح.")
    if direction=="BUY" and sl is not None and entry is not None and sl>=entry: vetoes.append("وقف BUY يجب أن يكون أسفل الدخول.")
    if direction=="SELL" and sl is not None and entry is not None and sl<=entry: vetoes.append("وقف SELL يجب أن يكون أعلى الدخول.")
    if direction=="BUY" and tp1 is not None and entry is not None and tp1<=entry: vetoes.append("TP1 في BUY يجب أن يكون أعلى الدخول.")
    if direction=="SELL" and tp1 is not None and entry is not None and tp1>=entry: vetoes.append("TP1 في SELL يجب أن يكون أسفل الدخول.")
    if rsi is not None and ((direction=="BUY" and rsi>=88) or (direction=="SELL" and rsi<=12)): vetoes.append(f"RSI متطرف جداً ({rsi:.1f}) ويشير إلى دخول شديد التأخر.")
    sup,opp=_smc_alignment(direction or "BUY",smc,note); oh=(direction=="BUY" and h4=="BEARISH") or (direction=="SELL" and h4=="BULLISH"); os_=(direction=="BUY" and state in {"BEARISH","STRONG_BEARISH"}) or (direction=="SELL" and state in {"BULLISH","STRONG_BULLISH"})
    if oh and os_ and opp: counter="مرتفع"; vetoes.append("H4 + HMM + SMC يدعمون الاتجاه المعاكس بقوة.")
    elif oh or os_ or opp: counter="متوسط"
    ctx=json.dumps({"signal":signal_data,"market":market_summary},ensure_ascii=False,default=str); hist,matched,lv=_historical_lesson_score(direction or "BUY",lessons or [],ctx); vetoes.extend(lv)
    structure=20 if sup else 10; trend=20 if ((direction=="BUY" and h4=="BULLISH") or (direction=="SELL" and h4=="BEARISH")) else 9; trend=min(trend,13) if state in {"RANGING","TRANSITION",""} else trend; entry_score=15 if sup and rr is not None and rr>=MIN_RR else 11 if sup else 8; rr_score=min(15,max(3,int((rr or 0)/3.0*15))) if rr is not None else 0; conf_score=max(2,min(10,int(confidence/10))); conf_score=max(2,conf_score-1) if confidence<MIN_CONFIDENCE else conf_score; momentum=10 if rsi is None else (8 if (direction=="BUY" and rsi<70) or (direction=="SELL" and rsi>30) else 5); momentum=3 if rsi is not None and ((direction=="BUY" and rsi>=76) or (direction=="SELL" and rsi<=24)) else momentum; liquidity=10 if any(_bool(smc.get(k)) for k in ("liquidity_bullish","liquidity_bearish","sweep_bullish","sweep_bearish")) else 5 if opp else 3; regime_score=5 if regime in {"TRENDING_BULLISH","TRENDING_BEARISH"} and ((direction=="BUY" and h4=="BULLISH") or (direction=="SELL" and h4=="BEARISH")) else 3
    scores={"structure":structure,"trend_alignment":trend,"entry_quality":entry_score,"risk_reward":rr_score,"confidence":conf_score,"momentum":momentum,"liquidity":liquidity,"regime_fit":regime_score,"historical_risk":hist}; total=max(0,min(100,sum(scores.values())))
    if vetoes: decision,approved,reason="REJECT",False,"فيتو مخاطر جوهري: "+" | ".join(vetoes[:3])
    elif total>=APPROVE_SCORE: decision,approved,reason="APPROVE",True,f"اجتازت الصفقة بوابة المخاطر بدرجة {total}/100؛ السياسة مرنة ولا تتطلب الكمال."
    elif total>=MODIFY_SCORE: decision,approved,reason="MODIFY",True,f"الصفقة قابلة للعمل بدرجة {total}/100؛ توجد ملاحظات تحسين لكنها ليست حظراً تلقائياً."
    else: decision,approved,reason="REJECT",False,f"جودة الصفقة منخفضة نسبياً ({total}/100) ولا يوجد دعم كافٍ للدخول الحالي."
    thesis=f"{direction or 'UNKNOWN'} مع H4={h4 or 'غير معروف'} وHMM={state or 'غير معروف'} وSMC={'مؤيد' if sup else 'ضعيف'} وRR={(f'{rr:.2f}' if rr is not None else 'غير متاح')}"; invalidation="كسر SL أو كسر بنية M15 مع تحول HMM/H4 ضد الصفقة."; reversal="BOS/CHoCH معاكس + تحول H4/HMM + تأكيد سيولة/FVG للاتجاه المقابل."
    return TradeReviewResult(approved,decision,total,reason,thesis,invalidation,reversal,regime,vetoes,matched,counter,scores)
def build_adversarial_prompt(signal_data,market_summary,deterministic,lessons):
    return "\n".join(["أنت مدير مخاطر كمي مرن متخصص في XAU/USD. افترض أن الصفقة قد تفشل وابحث عن الخطر الحقيقي، لكن لا ترفضها لمجرد أنها غير مثالية.","تعامل مع Gemini كمحلل معارض ومستشار، لا كحاجز تداول صارم. لا تتجاوز hard vetoes الحتمية. لا تخترع بيانات. أرجع JSON فقط.","التقييم الحتمي: "+json.dumps(deterministic.to_dict(),ensure_ascii=False,default=str),"الدروس: "+("\n".join("- "+str(x) for x in lessons) if lessons else "- لا توجد دروس"),"الإشارة: "+json.dumps(signal_data,ensure_ascii=False,default=str),"السوق: "+json.dumps(market_summary,ensure_ascii=False,default=str),"مثال الإخراج: {\"approved\": true, \"decision\": \"APPROVE\", \"reason\": \"سبب عربي مختصر\", \"thesis\": \"ملخص\", \"invalidation\": \"شرط البطلان\", \"reversal\": \"شرط الانعكاس\"}"])
def parse_ai_review(raw):
    if isinstance(raw,dict): return raw
    text=re.sub(r"^```(?:json)?\s*|\s*```$","",str(raw or "").strip(),flags=re.I|re.S).strip()
    try:
        v=json.loads(text); return v if isinstance(v,dict) else {}
    except (TypeError,ValueError): return {"approved":False,"decision":"INVALID","reason":"تعذر قراءة مراجعة Gemini المؤسسية."}
def apply_ai_review(deterministic,raw_ai):
    ai=parse_ai_review(raw_ai); ai_approved=_bool(ai.get("approved")); ai_decision=str(ai.get("decision") or ("APPROVE" if ai_approved else "REJECT")).upper(); deterministic.ai_review={**ai,"approved":ai_approved,"decision":ai_decision,"mode":"HARD_VETO" if AI_VETO_ENABLED else "ADVISORY"}
    if deterministic.hard_vetoes: return deterministic
    if (not ai_approved) or ai_decision in {"REJECT","REVERSE"}:
        if AI_VETO_ENABLED: deterministic.approved=False; deterministic.decision="REJECT"; deterministic.reason=str(ai.get("reason") or "مراجعة Gemini رأت خطراً إضافياً.")
        else: deterministic.reason=f"ملاحظة Gemini: {ai.get('reason') or 'تحفظ تحليلي'} — لم تُستخدم كفيتو تلقائي لأن الصفقة اجتازت الحماية الحتمية."
    else:
        if deterministic.approved and deterministic.decision=="MODIFY": deterministic.decision="APPROVE_WITH_CAUTION"
        deterministic.reason=str(ai.get("reason") or deterministic.reason)
    deterministic.thesis=str(ai.get("thesis") or deterministic.thesis); deterministic.invalidation=str(ai.get("invalidation") or deterministic.invalidation); deterministic.reversal=str(ai.get("reversal") or deterministic.reversal); return deterministic

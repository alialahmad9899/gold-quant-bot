"""Context-aware news semantics used by the live news runtime.

This layer does not invent a news direction from correlation alone. It uses article
context plus published Actual/Forecast/Previous values when present, and keeps
source quality separate from market direction.
"""
from __future__ import annotations
import re
from dataclasses import replace
from typing import Any

POSITIVE=("rate cut","cuts rates","dovish","easing","lower rates","weaker dollar","dollar falls","falling yields","safe haven","war escalates","escalation","sanction","inflation rises","gold rises","gold gains")
NEGATIVE=("rate hike","hikes rates","hawkish","higher rates","strong dollar","dollar rises","rising yields","yield rises","inflation cools","ceasefire","gold falls","gold drops")
MACRO=("cpi","inflation","pce","nfp","nonfarm","payroll","jobs","employment")

def parse_surprise(text:str)->dict[str,float|None]:
    patterns={"actual":r"(?:actual|reported|released)\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*%?","forecast":r"(?:forecast|expected|estimate)\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*%?","previous":r"(?:previous|prior)\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*%?"}
    out={k:None for k in patterns}
    for k,p in patterns.items():
        m=re.search(p,text,re.I)
        if m:
            try: out[k]=float(m.group(1))
            except ValueError: pass
    out["surprise"]=(out["actual"]-out["forecast"]) if out["actual"] is not None and out["forecast"] is not None else None
    return out

def source_quality(source:str)->int:
    s=str(source or "").lower()
    if "reuters" in s or "bloomberg" in s: return 95
    if "cnbc" in s or "bbc" in s: return 88
    if "federalreserve.gov" in s or "treasury.gov" in s or "bls.gov" in s: return 100
    if "gdelt" in s: return 70
    return 60

def classify(article, base_classifier):
    text=f"{getattr(article,'title','')} {getattr(article,'summary','')}".lower()
    base=base_classifier(article)
    bull=sum(1 for p in POSITIVE if p in text); bear=sum(1 for p in NEGATIVE if p in text); surprise=parse_surprise(text)
    if surprise["surprise"] is not None and any(k in text for k in MACRO):
        s=float(surprise["surprise"])
        if s>0: bear+=2
        elif s<0: bull+=2
    if bull==bear: direction="NEUTRAL"
    else: direction="BULLISH_GOLD" if bull>bear else "BEARISH_GOLD"
    impact=max(int(base.impact),45+15*min(3,abs(bull-bear))+(15 if surprise["surprise"] is not None else 0)) if direction!="NEUTRAL" else min(int(base.impact),30)
    confidence=min(95,max(int(base.confidence),50+10*min(3,abs(bull-bear))+(10 if surprise["surprise"] is not None else 0)))
    reasons=list(base.reasons)
    if surprise["surprise"] is not None: reasons.append(f"مفاجأة اقتصادية محسوبة: {surprise['surprise']:+.3f}")
    quality=source_quality(getattr(article,'source',''))
    reasons.append(f"موثوقية المصدر التقريبية: {quality}/100")
    return replace(base,direction=direction,impact=min(100,impact),confidence=min(95,confidence),reasons=reasons,material=(direction!="NEUTRAL" and impact>=45))

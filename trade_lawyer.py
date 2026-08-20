"""Flexible trade-lawyer plus live-news advisory bridge for XAU/USD."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import CallbackQueryHandler, CommandHandler
except Exception:
    InlineKeyboardButton = InlineKeyboardMarkup = None
    CallbackQueryHandler = CommandHandler = None

from news_intelligence import NewsArticle, NewsDecision, NewsIntelligence


@dataclass
class TradeLawyerDecision:
    decision: str
    score: int
    reasons: list[str]
    warnings: list[str]


def review_trade(signal: dict, context: dict | None = None) -> TradeLawyerDecision:
    context = context or {}
    score = 50
    reasons: list[str] = []
    warnings: list[str] = []
    entry = float(signal.get("entry", 0) or 0); sl = float(signal.get("sl", 0) or 0)
    tp1 = float(signal.get("tp1", 0) or 0); tp2 = float(signal.get("tp2", 0) or 0)
    if entry and sl:
        if abs(entry - sl) > 0: score += 5; reasons.append("SL distance valid")
        else: score -= 20; warnings.append("Invalid SL distance")
    rr = abs(tp1 - entry) / abs(entry - sl) if entry and tp1 and sl and abs(entry - sl) > 0 else float(signal.get("risk_reward", 0) or 0)
    if rr >= 2: score += 15; reasons.append("Risk reward acceptable")
    elif rr < 1: score -= 8; warnings.append("Risk reward weak; advisory only")
    if tp2 and tp1: reasons.append("TP structure validated")
    for key, points, text in [("bos_confirmed",10,"BOS confirmed"),("choch_confirmed",8,"CHoCH confirmed"),("fvg_valid",8,"FVG valid"),("h4_aligned",10,"H4 trend aligned")]:
        if signal.get(key): score += points; reasons.append(text)
    if context.get("similar_trade_open"): score -= 8; warnings.append("Similar active trade exists")
    if context.get("near_major_zone"): score -= 4; warnings.append("Near important price zone")
    if context.get("live_quote_valid") is False: score -= 30; warnings.append("Live quote unavailable")
    if context.get("data_quality") not in (None,"OK","HISTORICAL_HEALTHY"): score -= 15; warnings.append("Data quality warning")
    score = max(0, min(100, score))
    decision = "REJECT" if score < 25 else "MODIFY" if score < 55 else "APPROVE"
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
    def to_dict(self) -> dict[str, Any]: return asdict(self)

AI_INTERVAL_SECONDS = float(os.getenv("TRADE_LAWYER_AI_INTERVAL_SECONDS", "900"))
ADD_SCORE_THRESHOLD = int(os.getenv("TRADE_LAWYER_MIN_ADD_SCORE", "68"))
LAWYER_NOTIFICATION_MIN_SECONDS = float(os.getenv("TRADE_LAWYER_NOTIFICATION_MIN_SECONDS", "120"))
NEWS_NOTIFICATION_MIN_SECONDS = float(os.getenv("NEWS_NOTIFICATION_MIN_SECONDS", "120"))


def _direction(value: Any) -> str:
    raw = str(value or "").upper()
    if "BUY" in raw or "شراء" in raw: return "BUY"
    if "SELL" in raw or "بيع" in raw: return "SELL"
    return "UNKNOWN"


def _r_multiple(direction: str, entry: float, price: float, sl: float | None) -> float | None:
    if sl is None or abs(entry-sl) <= 0: return None
    return (price-entry if direction == "BUY" else entry-price) / abs(entry-sl)


def deterministic_active_advice(trade: dict[str, Any], market: dict[str, Any], management: dict[str, Any], review: dict[str, Any] | None = None) -> ActiveTradeLawyerAdvice:
    direction = _direction(trade.get("direction")); entry = float(trade.get("entry") or 0); price = float(market.get("price") or entry)
    sl = float(trade["sl"]) if trade.get("sl") is not None else None; tp1 = float(trade["tp1"]) if trade.get("tp1") is not None else None; tp2 = float(trade["tp2"]) if trade.get("tp2") is not None else None
    r = _r_multiple(direction, entry, price, sl); state = str(management.get("state") or "ACTIVE"); lifecycle = str(management.get("decision") or "KEEP"); risk_score = int((review or {}).get("risk_score") or 0)
    news = market.get("news_decision") or {}
    if news.get("conflict") and news.get("action") == "EXIT":
        return ActiveTradeLawyerAdvice("EXIT","URGENT",96,"خبر عالي التأثير جاء ضد الصفقة وترافق مع تأكيد سعري معاكس؛ أعد التقييم فوراً.","NEWS_STRESSED",avoid_condition="لا تعزز قبل إعادة التقييم.")
    if news.get("conflict") and news.get("action") == "REDUCE_RISK":
        return ActiveTradeLawyerAdvice("REDUCE_RISK","HIGH",90,"خبر مؤثر ضد الصفقة؛ خفّض التعرض وراقب تأكيد البنية بدلاً من الخروج الآلي.","NEWS_STRESSED",avoid_condition="لا توسع SL لإنقاذ الصفقة.")
    if state == "INVALIDATED" or lifecycle == "REVERSE": return ActiveTradeLawyerAdvice("EXIT","URGENT",99,"الثيسس فقدت صلاحيتها أو ظهرت شروط انعكاس قوية؛ القرار النهائي يمر بمحرك المخاطر.","INVALIDATED",avoid_condition="لا تعزز قبل حسم الانعكاس.")
    if state == "TP1_REACHED": return ActiveTradeLawyerAdvice("PROTECT_PROFIT","HIGH",94,"تحقق TP1؛ الأفضل حماية الربح تدريجياً لا الإغلاق الكامل تلقائياً.","INTACT",recommended_sl=entry,recommended_tp1=tp1,recommended_tp2=tp2)
    if state == "UNDER_PRESSURE" or (r is not None and r <= -0.50): return ActiveTradeLawyerAdvice("REDUCE_RISK","HIGH",88,"الصفقة تحت ضغط واضح لكن ذلك لا يعني الخروج الفوري؛ لا توسع SL لإنقاذها.","STRESSED",add_condition="عودة البنية مع اتجاه الصفقة.",avoid_condition="عدم توسيع SL.")
    if r is not None and r >= 0.75 and risk_score >= ADD_SCORE_THRESHOLD and state == "ACTIVE": return ActiveTradeLawyerAdvice("ADD_ON_CONFIRMATION","MEDIUM",72,"يمكن التفكير بإضافة صغيرة فقط بعد pullback وتأكيد جديد، وليس بمطاردة السعر.","INTACT",add_condition="SMC confirmation جديد + RR مناسب.",avoid_condition="لا تضف بعد اندفاع ممتد.")
    return ActiveTradeLawyerAdvice("HOLD","LOW",80,"الثيسس ما زالت قابلة للحياة ولا يوجد سبب قوي للخروج أو التعزيز الفوري.","INTACT",recommended_sl=sl,recommended_tp1=tp1,recommended_tp2=tp2)


def build_active_trade_prompt(trade, market, management, review, deterministic):
    return f'''أنت «محامي الصفقة» لصفقة XAU/USD مفتوحة. أنت مستشار مرن لا بوابة صارمة. HOLD هو الطبيعي ما دامت thesis سليمة. لا تخرج بسبب ضوضاء قصيرة، ولا توسع SL. بعد TP1 احمِ الربح تدريجياً. التعزيز مشروط بتأكيد جديد وعدم مطاردة السعر. EXIT/REVERSE فقط عند دليل واضح على invalidation. لا تخترع بيانات. الأفعال: HOLD, PROTECT_PROFIT, REDUCE_RISK, ADD_ON_CONFIRMATION, EXIT, PREPARE_REVERSAL.\n\nالحتمي: {json.dumps(deterministic.to_dict(),ensure_ascii=False)}\nالصفقة: {json.dumps(trade,ensure_ascii=False)}\nالسوق: {json.dumps(market,ensure_ascii=False)}\nالإدارة: {json.dumps(management,ensure_ascii=False)}\nالمراجعة: {json.dumps(review or {},ensure_ascii=False)}\n\nأرجع JSON فقط: {{"action":"HOLD","urgency":"LOW","confidence":80,"reason":"سبب عربي","thesis_status":"INTACT","recommended_sl":null,"recommended_tp1":null,"recommended_tp2":null,"add_condition":"","avoid_condition":""}}'''


def parse_ai(raw: Any) -> dict[str, Any]:
    if isinstance(raw,dict): return raw
    text=str(raw or "").strip().replace("```json","").replace("```","").strip()
    try:
        value=json.loads(text); return value if isinstance(value,dict) else {}
    except Exception: return {}


def apply_active_ai(deterministic, raw, model=""):
    ai=parse_ai(raw); action=str(ai.get("action") or deterministic.action).upper(); allowed={"HOLD","PROTECT_PROFIT","REDUCE_RISK","ADD_ON_CONFIRMATION","EXIT","PREPARE_REVERSAL"}
    if action not in allowed or deterministic.action == "EXIT": return deterministic
    if action == "EXIT" and deterministic.action not in {"REDUCE_RISK","PROTECT_PROFIT"} and str(ai.get("thesis_status") or "INTACT").upper()=="INTACT": action="HOLD"
    try: confidence=int(max(0,min(100,float(ai.get("confidence",deterministic.confidence)))))
    except (TypeError,ValueError): confidence=deterministic.confidence
    return ActiveTradeLawyerAdvice(action,str(ai.get("urgency") or deterministic.urgency).upper(),confidence,str(ai.get("reason") or deterministic.reason),str(ai.get("thesis_status") or deterministic.thesis_status).upper(),ai.get("recommended_sl",deterministic.recommended_sl),ai.get("recommended_tp1",deterministic.recommended_tp1),ai.get("recommended_tp2",deterministic.recommended_tp2),str(ai.get("add_condition") or deterministic.add_condition),str(ai.get("avoid_condition") or deterministic.avoid_condition),True,model)


def should_call_active_ai(last_call, state, previous_state):
    if previous_state and previous_state != state: return True
    return last_call is None or (time.monotonic()-last_call)>=AI_INTERVAL_SECONDS


def _get_bot_module(): return sys.modules.get("bot") or sys.modules.get("__main__")

def _lawyer_snapshot(bot):
    cache=getattr(bot,"GLOBAL_CACHE",{})
    return {"advice":dict(cache.get("trade_lawyer") or {}) if isinstance(cache,dict) else {},"management":dict(cache.get("active_trade_management") or {}) if isinstance(cache,dict) else {}}


def format_lawyer_message(snapshot):
    advice=snapshot.get("advice") or {}; management=snapshot.get("management") or {}; action=str(advice.get("action") or "HOLD").upper(); labels={"HOLD":"الاحتفاظ بالصفقة","PROTECT_PROFIT":"حماية الأرباح","REDUCE_RISK":"تخفيف المخاطرة","ADD_ON_CONFIRMATION":"تعزيز مشروط","EXIT":"الخروج","PREPARE_REVERSAL":"الاستعداد للانعكاس"}
    return f"🧑‍⚖️ **محامي الصفقة**\n───────────────────\n📌 **الحكم:** {labels.get(action,action)}\n⚡ **الاستعجال:** {advice.get('urgency','LOW')}\n🎯 **الثقة:** {advice.get('confidence','-')}%\n📊 **الحالة:** {management.get('state') or advice.get('thesis_status') or 'ACTIVE'}\n\n💡 **الرأي:** {advice.get('reason') or 'لا يوجد تغيير جوهري.'}\n🛡️ **تجنب:** {advice.get('avoid_condition') or 'لا يوجد.'}\n➕ **التعزيز:** {advice.get('add_condition') or 'لا يوجد.'}"


def _lawyer_keyboard():
    if InlineKeyboardButton is None or InlineKeyboardMarkup is None: return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧑‍⚖️ استشر محامي الصفقة الآن",callback_data="trade_lawyer_now")],
        [InlineKeyboardButton("📰 آخر أخبار الذهب",callback_data="news_now")],
    ])

async def _lawyer_command(update, context):
    bot=_get_bot_module();
    if bot is None: return
    chat_id=update.effective_chat.id
    if hasattr(bot,"is_authenticated") and not bot.is_authenticated(chat_id): await bot.safe_reply_text(update,"🔒 يرجى تسجيل الدخول أولاً."); return
    snap=_lawyer_snapshot(bot)
    if not snap["advice"]: await bot.safe_reply_text(update,"🧑‍⚖️ لا توجد صفقة نشطة حالياً تحتاج إلى مراجعة."); return
    await bot.safe_reply_text(update,format_lawyer_message(snap),reply_markup=_lawyer_keyboard(),parse_mode="Markdown")

async def _lawyer_callback(update, context):
    query=update.callback_query; await query.answer(); bot=_get_bot_module()
    if bot is None: return
    await query.message.reply_text(format_lawyer_message(_lawyer_snapshot(bot)),reply_markup=_lawyer_keyboard(),parse_mode="Markdown")

async def _news_command(update, context):
    bot=_get_bot_module()
    if bot is None: return
    chat_id=update.effective_chat.id
    if hasattr(bot,"is_authenticated") and not bot.is_authenticated(chat_id):
        await bot.safe_reply_text(update,"🔒 يرجى تسجيل الدخول أولاً.")
        return
    decision=_news_context(bot)
    cache=getattr(bot,"GLOBAL_CACHE",{})
    latest=(cache.get("latest_news") or []) if isinstance(cache,dict) else []
    if decision:
        text=f"📰 **ذكاء الأخبار اللحظي**\n\n📌 **القرار:** {decision.action}\n🎯 **التأثير:** {decision.impact}/100\n⚡ **الاستعجال:** {decision.urgency}\n\n💡 {decision.reason}"
    elif latest:
        text="📰 **آخر الأخبار**\n\n" + "\n".join(f"• {item.get('title','')}" for item in latest[:5])
    else:
        text="📰 لا يوجد حالياً خبر جديد مؤثر تم التقاطه."
    await bot.safe_reply_text(update,text,reply_markup=_lawyer_keyboard(),parse_mode="Markdown")

async def _news_callback(update, context):
    query=update.callback_query
    await query.answer()
    await _news_command(query, context)

async def _broadcast_lawyer_update(app,bot,snapshot):
    if not hasattr(bot,"get_subscribers"): return
    for uid in bot.get_subscribers():
        try:
            if hasattr(bot,"is_authenticated") and not bot.is_authenticated(uid): continue
            await bot.safe_send_message(app.bot,chat_id=uid,text=format_lawyer_message(snapshot),parse_mode="Markdown",reply_markup=_lawyer_keyboard())
        except Exception: pass

def install_telegram_trade_lawyer(app,bot):
    if getattr(app,"_trade_lawyer_handlers_installed",False): return
    if CommandHandler is not None:
        app.add_handler(CommandHandler("lawyer",_lawyer_command))
        app.add_handler(CommandHandler("news",_news_command))
    if CallbackQueryHandler is not None:
        app.add_handler(CallbackQueryHandler(_lawyer_callback,pattern=r"^trade_lawyer_now$"))
        app.add_handler(CallbackQueryHandler(_news_callback,pattern=r"^news_now$"))
    app._trade_lawyer_handlers_installed=True


# ---- Live news bridge ----------------------------------------------------
_news = NewsIntelligence()
_news_last_signature = None
_news_last_notification = 0.0
_news_last_poll = 0.0


def _price(bot):
    try:
        market=bot.get_market_data() or {}; feed=market.get("price_feed") or {}; return float(feed.get("mid") or feed.get("spot") or market.get("gold"))
    except Exception: return None


def _news_context(bot, active_direction=None):
    global _news_last_poll
    if time.monotonic()-_news_last_poll < max(10,_news.poll_seconds): return None
    _news_last_poll=time.monotonic(); articles=_news.fetch_latest()
    if not articles: return None
    price=_price(bot); cache=getattr(bot,"GLOBAL_CACHE",{})
    previous=float(cache.get("news_reference_price") or price or 0) if isinstance(cache,dict) else (price or 0)
    pct=((price-previous)/previous*100) if price and previous else 0.0
    price_dir="UP" if pct>0 else "DOWN" if pct<0 else "FLAT"
    if isinstance(cache,dict): cache["news_reference_price"]=price or previous; cache["latest_news"]=[asdict(a) for a in articles[:10]]
    decisions=[]
    for a in articles:
        d=_news.evaluate_active_trade(active_direction,a,pct,price_dir) if active_direction else _news.evaluate_news_entry(a,pct,price_dir)
        if d.action not in {"NO_TRADE","REASSESS"}: decisions.append(d)
    if not decisions: return None
    decision=max(decisions,key=lambda x:(x.impact,x.confidence))
    if isinstance(cache,dict): cache["news_decision"]=asdict(decision)
    return decision


def _news_candidate(bot, decision):
    price=_price(bot)
    if not price or decision.action not in {"NEWS_BUY","NEWS_SELL"}: return None
    risk_pct=float(os.getenv("NEWS_ENTRY_RISK_PCT","0.0015")); tp1_pct=float(os.getenv("NEWS_ENTRY_TP1_PCT","0.0025")); tp2_pct=float(os.getenv("NEWS_ENTRY_TP2_PCT","0.0040"))
    buy=decision.action=="NEWS_BUY"; sl=price*(1-risk_pct) if buy else price*(1+risk_pct); tp1=price*(1+tp1_pct) if buy else price*(1-tp1_pct); tp2=price*(1+tp2_pct) if buy else price*(1-tp2_pct)
    return {"status":"SIGNAL","type":"📰 شراء خبري فوري" if buy else "📰 بيع خبري فوري","direction":"BUY" if buy else "SELL","entry":price,"sl":sl,"tp1":tp1,"tp2":tp2,"confidence":decision.confidence,"smc_note":f"News Intelligence: {decision.reason}","news_driven":True,"news_impact":decision.impact,"news_urgency":decision.urgency,"reason":"إشارة خبرية مؤكدة بحركة السعر."}


def get_live_news_context(bot, active_direction=None):
    return _news_context(bot, active_direction)


def get_news_candidate(bot, decision):
    return _news_candidate(bot, decision)


def _install_news_hooks(bot):
    if getattr(bot,"_news_hooks_installed",False): return
    original_generate=getattr(bot,"generate_quant_signal",None); original_monitor=getattr(bot,"monitor_open_trades",None)
    if original_generate is None: return
    def wrapped_generate(*args,**kwargs):
        active=bool(getattr(bot,"has_active_open_trade",lambda *_:False)("BUY") or getattr(bot,"has_active_open_trade",lambda *_:False)("SELL"))
        if active:
            return original_generate(*args,**kwargs)
        # Technical pipeline gets first opportunity. News is an additional trigger, not a replacement.
        result=original_generate(*args,**kwargs)
        if isinstance(result,dict) and result.get("status")=="SIGNAL":
            return result
        if os.getenv("NEWS_SIGNAL_ENABLED","1")!="1":
            return result
        decision=_news_context(bot)
        candidate=_news_candidate(bot,decision) if decision else None
        if candidate and isinstance(getattr(bot,"GLOBAL_CACHE",None),dict):
            bot.GLOBAL_CACHE["news_candidate"]=candidate
        return result
    bot.generate_quant_signal=wrapped_generate
    if original_monitor is not None:
        def wrapped_monitor(*args,**kwargs):
            result=original_monitor(*args,**kwargs)
            try:
                active_dir=None
                cache=getattr(bot,"GLOBAL_CACHE",{})
                thesis=(cache.get("active_trade") or {}) if isinstance(cache,dict) else {}
                active_dir=thesis.get("direction")
                if active_dir: _news_context(bot,active_dir)
            except Exception: pass
            return result
        bot.monitor_open_trades=wrapped_monitor
    bot._news_hooks_installed=True


def _news_worker():
    global _news_last_signature,_news_last_notification
    while True:
        try:
            bot=_get_bot_module(); app=getattr(bot,"app",None) if bot else None
            if bot:
                _install_news_hooks(bot)
                decision=_news_context(bot)
                if decision and isinstance(getattr(bot,"GLOBAL_CACHE",None),dict):
                    bot.GLOBAL_CACHE["news_decision"]=asdict(decision)
                    sig=(decision.action,decision.direction,decision.impact)
                    if app and sig!=_news_last_signature and time.monotonic()-_news_last_notification>=NEWS_NOTIFICATION_MIN_SECONDS and hasattr(bot,"get_subscribers"):
                        text=f"📰 **ذكاء الأخبار**\n\n📌 {decision.action}\n🎯 التأثير على الذهب: {decision.impact}/100\n⚡ الاستعجال: {decision.urgency}\n\n💡 {decision.reason}"
                        if hasattr(app,"create_task"):
                            for uid in bot.get_subscribers():
                                try: app.create_task(bot.safe_send_message(app.bot,chat_id=uid,text=text,parse_mode="Markdown",reply_markup=_lawyer_keyboard()))
                                except Exception: pass
                        _news_last_signature=sig; _news_last_notification=time.monotonic()
                install_telegram_trade_lawyer(app,bot) if app else None
            time.sleep(max(5, int(os.getenv("NEWS_WORKER_SLEEP_SECONDS","10"))))
        except Exception:
            time.sleep(5)

threading.Thread(target=_news_worker,name="gold-news-intelligence",daemon=True).start()

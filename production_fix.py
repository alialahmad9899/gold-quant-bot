"""Final production hardening for the canonical XAU/USD runtime."""
from __future__ import annotations
import logging, math, os, threading, sys
from datetime import datetime, timezone

LOGGER=logging.getLogger("XAUUSD_QuantBot.ProductionFix")
_LOCK=threading.RLock(); _INSTALLED=False; _APPLICATION_PATCHED=False; _GENERATION_PATCHED=False
PRICE_SANITY_MAX_PCT=float(os.getenv("FINAL_PRICE_SANITY_MAX_PCT","0.0030"))


def _is_pg(bot,conn):
    try: return bool(getattr(bot,"is_postgres",lambda:False)())
    except Exception: return False

def _ensure_schema(bot):
    conn=None
    try:
        conn=bot.get_db_connection(); cur=conn.cursor()
        if _is_pg(bot,conn):
            cur.execute("CREATE TABLE IF NOT EXISTS runtime_decision_events (id BIGSERIAL PRIMARY KEY,candle_id TEXT,direction TEXT,final_decision TEXT,decision_state TEXT,confidence REAL,entry REAL,live_price REAL,price_gap_pct REAL,ai_advisory BOOLEAN,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runtime_decision_events_created ON runtime_decision_events(created_at)")
        else:
            cur.execute("CREATE TABLE IF NOT EXISTS runtime_decision_events (id INTEGER PRIMARY KEY AUTOINCREMENT,candle_id TEXT,direction TEXT,final_decision TEXT,decision_state TEXT,confidence REAL,entry REAL,live_price REAL,price_gap_pct REAL,ai_advisory INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runtime_decision_events_created ON runtime_decision_events(created_at)")
        conn.commit()
    except Exception as exc:
        LOGGER.warning("runtime audit schema: %s",exc)
        try:
            if conn: conn.rollback()
        except Exception: pass
    finally:
        if conn is not None:
            try: bot.release_db_connection(conn)
            except Exception:
                try: conn.close()
                except Exception: pass

def _db_event(bot,result,live_price,gap_pct):
    conn=None
    try:
        conn=bot.get_db_connection(); cur=conn.cursor(); pg=_is_pg(bot,conn); sql="INSERT INTO runtime_decision_events(candle_id,direction,final_decision,decision_state,confidence,entry,live_price,price_gap_pct,ai_advisory) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)" if pg else "INSERT INTO runtime_decision_events(candle_id,direction,final_decision,decision_state,confidence,entry,live_price,price_gap_pct,ai_advisory) VALUES (?,?,?,?,?,?,?,?,?)"; raw=str(result.get("type") or ""); direction="BUY" if "شراء" in raw else "SELL" if "بيع" in raw else str(result.get("direction") or "UNKNOWN"); cur.execute(sql,(str(result.get("candle_id") or ""),direction,str(result.get("final_decision") or ""),str(result.get("decision_state") or ""),float(result.get("confidence") or 0.0),float(result.get("entry") or 0.0),live_price,gap_pct,bool(result.get("ai_advisory")))); conn.commit()
    except Exception as exc:
        LOGGER.warning("runtime decision audit write failed: %s",exc)
        try:
            if conn: conn.rollback()
        except Exception: pass
    finally:
        if conn is not None:
            try: bot.release_db_connection(conn)
            except Exception:
                try: conn.close()
                except Exception: pass

def _finite_price(value):
    try:
        value=float(value); return value if value>0 and math.isfinite(value) else None
    except (TypeError,ValueError): return None

def _live_price(bot):
    try:
        market=bot.get_market_data() or {}; feed=market.get("price_feed") or {}
        for raw in (feed.get("mid"),feed.get("spot"),market.get("gold")):
            value=_finite_price(raw)
            if value is not None: return value
    except Exception: pass
    try:
        sitecustomize=sys.modules.get("sitecustomize")
        if sitecustomize is not None:
            quote=sitecustomize.get_websocket_quote(); return _finite_price((quote or {}).get("price"))
    except Exception: pass
    return None

def _normalize_numeric(result):
    for key in ("entry","sl","tp1","tp2","rr","dxy_corr"):
        value=result.get(key)
        if value is None: continue
        try:
            if not math.isfinite(float(value)): result[key]=None
        except (TypeError,ValueError): result[key]=None
    if result.get("dxy_corr") is None:
        result["dxy_corr_data_quality"]="UNKNOWN"; result["dxy_trend"]="UNKNOWN"; result["dxy_pressure"]="NEUTRAL"

def _apply_price_sanity(bot,result):
    _normalize_numeric(result); live=_live_price(bot); entry=result.get("entry")
    if live is not None and entry is not None:
        try:
            gap=abs(float(entry)-live)/live; result["live_price_at_decision"]=round(live,4); result["entry_price_gap_pct"]=round(gap*100.0,4)
            if gap>PRICE_SANITY_MAX_PCT:
                return {"status":"WAIT","decision_state":"PRICE_SANITY_FAIL","candidate":False,"reason":f"سعر الإشارة غير متطابق مع السعر الحي: فرق {gap*100:.3f}% يتجاوز الحد الآمن {PRICE_SANITY_MAX_PCT*100:.3f}%. ","price":live}
        except (TypeError,ValueError):
            return {"status":"WAIT","decision_state":"PRICE_SANITY_FAIL","candidate":False,"reason":"تعذر التحقق من تطابق سعر الإشارة مع السعر الحي.","price":live}
    result.setdefault("candidate",True); result.setdefault("decision_state","TRADE_READY"); return result

def _patch_generation(bot):
    global _GENERATION_PATCHED
    current=getattr(bot,"generate_quant_signal",None)
    if current is None: return
    if getattr(current,"_production_fix",False): _GENERATION_PATCHED=True; return
    def wrapped(*args,**kwargs):
        result=current(*args,**kwargs)
        if not isinstance(result,dict) or result.get("status")!="SIGNAL": return result
        result=_apply_price_sanity(bot,dict(result))
        if result.get("status")!="SIGNAL": return result
        confidence=float(result.get("confidence") or 0.0); max_score=max(float(result.get("score_bull") or 0.0),float(result.get("score_bear") or 0.0))
        if confidence<40.0 and max_score<6.0:
            return {"status":"WAIT","decision_state":"WATCH","candidate":True,"reason":f"فرصة تحت المراقبة فقط: الثقة {confidence:.0f}% مع قوة اتجاهية {max_score:.2f} غير كافية للدخول.","price":result.get("entry",0.0),"candle_id":result.get("candle_id")}
        if result.get("ai_advisory"):
            result["final_decision"]="APPROVE_WITH_CAUTION"; result["final_reason"]="Gemini متحفظ؛ القرار الكمي سمح بالدخول بحذر بعد اجتياز فحوص البيانات والمخاطر."
        result["decision_state"]="TRADE_READY"; result["candidate"]=True; _db_event(bot,result,result.get("live_price_at_decision"),result.get("entry_price_gap_pct")); return result
    wrapped._production_fix=True; bot.generate_quant_signal=wrapped; _GENERATION_PATCHED=True

def _keyboard_patch(bot):
    original=getattr(bot,"get_main_keyboard",None)
    if original is None or getattr(original,"_production_fix",False): return
    def keyboard():
        try:
            from telegram import KeyboardButton,ReplyKeyboardMarkup
            base=original(); rows=[list(row) for row in (getattr(base,"keyboard",None) or [])]; labels={str(cell.text) for row in rows for cell in row if hasattr(cell,"text")}
            if "🧑‍⚖️ محامي الصفقة" not in labels or "📰 أخبار الذهب" not in labels: rows.append([KeyboardButton("🧑‍⚖️ محامي الصفقة"),KeyboardButton("📰 أخبار الذهب")])
            return ReplyKeyboardMarkup(rows,resize_keyboard=True,is_persistent=True)
        except Exception: return original()
    keyboard._production_fix=True; bot.get_main_keyboard=keyboard

def _active_trade(bot):
    integration=getattr(bot,"_phase2_runtime_integration",None)
    try:
        trade=getattr(getattr(integration,"manager",None),"active_trade",None)
        if trade: return trade
    except Exception: pass
    return None

def _lawyer_snapshot(bot):
    conn=None
    try:
        trade=_active_trade(bot)
        if trade is not None:
            thesis=trade.thesis; market=_live_price(bot) or thesis.entry; direction=str(thesis.direction).upper(); entry=float(thesis.entry); sl=float(thesis.sl or entry); tp1=float(thesis.tp1 or entry); tp2=float(thesis.tp2 or entry); action="HOLD"; reason="الصفقة ما زالت ضمن منطقة الإدارة الطبيعية."
            if direction=="BUY":
                if market>=tp1: action="PROTECT_PROFIT"; reason="تم بلوغ TP1؛ حماية الربح أولاً."
                elif market<=sl: action="EXIT"; reason="السعر وصل إلى SL."
                elif market<=entry-0.5*abs(entry-sl): action="REDUCE_RISK"; reason="ضغط سعري متوسط؛ تخفيف المخاطرة دون خروج آلي."
            else:
                if market<=tp1: action="PROTECT_PROFIT"; reason="تم بلوغ TP1؛ حماية الربح أولاً."
                elif market>=sl: action="EXIT"; reason="السعر وصل إلى SL."
                elif market>=entry+0.5*abs(entry-sl): action="REDUCE_RISK"; reason="ضغط سعري متوسط؛ تخفيف المخاطرة دون خروج آلي."
            return f"🧑‍⚖️ محامي الصفقة\n\nالصفقة: {'شراء' if direction=='BUY' else 'بيع'} | الحالة: {getattr(trade,'state','ACTIVE')}\nالسعر الحالي: ${market:.2f}\nالدخول: ${entry:.2f}\nSL: ${sl:.2f}\nTP1: ${tp1:.2f}\nTP2: ${tp2:.2f}\n\nالحكم: {action}\nالقرار: {reason}\n\nالمحامي مستشار مرن ولا يفتح صفقة عكسية تلقائياً."
        conn=bot.get_db_connection(); cur=conn.cursor(); cur.execute("SELECT signal_type,entry_price,sl,tp1,tp2,trade_status FROM trades WHERE outcome IS NULL AND trade_status IN ('OPEN','TP1_HIT') ORDER BY id DESC LIMIT 1"); row=cur.fetchone()
        if not row: return "🧑‍⚖️ لا توجد صفقة نشطة حالياً تحتاج إلى مراجعة."
        sig,entry,sl,tp1,tp2,status=row; price=_live_price(bot) or float(entry); direction="BUY" if "BUY" in str(sig).upper() or "شراء" in str(sig) else "SELL"; action="HOLD"; reason="الصفقة ما زالت ضمن منطقة الإدارة الطبيعية."; entry=float(entry); sl=float(sl); tp1=float(tp1); tp2=float(tp2)
        if direction=="BUY":
            if price>=tp1: action="PROTECT_PROFIT"; reason="تم بلوغ TP1؛ حماية الربح أولاً."
            elif price<=sl: action="EXIT"; reason="السعر وصل إلى SL."
        else:
            if price<=tp1: action="PROTECT_PROFIT"; reason="تم بلوغ TP1؛ حماية الربح أولاً."
            elif price>=sl: action="EXIT"; reason="السعر وصل إلى SL."
        return f"🧑‍⚖️ محامي الصفقة\n\nالصفقة: {'شراء' if direction=='BUY' else 'بيع'} | الحالة: {status}\nالسعر الحالي: ${price:.2f}\nالدخول: ${entry:.2f}\nSL: ${sl:.2f}\nTP1: ${tp1:.2f}\nTP2: ${tp2:.2f}\n\nالحكم: {action}\nالقرار: {reason}"
    except Exception as exc: return f"🧑‍⚖️ تعذر تحديث المحامي حالياً: {type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try: bot.release_db_connection(conn)
            except Exception: pass

def _parse_direction(raw):
    value=str(raw or "").upper()
    if "BUY" in value or "شراء" in value: return "BUY"
    if "SELL" in value or "بيع" in value: return "SELL"
    return "UNKNOWN"

def _stats_message(bot):
    conn=None
    try:
        conn=bot.get_db_connection(); cur=conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trades")
        trade_total=int(cur.fetchone()[0] or 0)
        cur.execute("SELECT signal_type, COUNT(*) FROM trades GROUP BY signal_type")
        trade_rows=cur.fetchall()
        trade_buy=sum(int(count or 0) for signal_type,count in trade_rows if _parse_direction(signal_type)=="BUY")
        trade_sell=sum(int(count or 0) for signal_type,count in trade_rows if _parse_direction(signal_type)=="SELL")
        cur.execute("SELECT COUNT(*) FROM runtime_decision_events")
        audit_total=int(cur.fetchone()[0] or 0)
        cur.execute("SELECT COUNT(*) FROM trades WHERE outcome IS NULL AND trade_status IN ('OPEN','TP1_HIT')")
        active=int(cur.fetchone()[0] or 0)
        cur.execute("SELECT COUNT(*) FROM trades WHERE outcome IS NOT NULL")
        closed=int(cur.fetchone()[0] or 0)
        return ("📊 تقرير النظام\n"
                f"الإشارات المسجلة فعلياً: {trade_total}\n"
                f"BUY: {trade_buy}\n"
                f"SELL: {trade_sell}\n"
                f"الصفقات المغلقة: {closed}\n"
                f"الصفقات النشطة: {active}\n"
                f"أحداث تدقيق القرار: {audit_total}\n\n"
                "المصدر المرجعي للإشارات هو سجل الصفقات المحفوظ؛ أحداث القرار تستخدم للتدقيق وليست بديلاً عنه.")
    except Exception as exc: return f"⚠️ تعذر قراءة تقرير النظام حالياً: {type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try: bot.release_db_connection(conn)
            except Exception: pass

def _authorized(bot,update):
    try: return bool(bot.is_authenticated(update.effective_chat.id))
    except Exception: return False

def _install_application_router(bot):
    global _APPLICATION_PATCHED
    try: from telegram.ext import Application
    except Exception as exc: LOGGER.warning("Telegram Application unavailable: %s",exc); return
    with _LOCK:
        current=getattr(Application,"process_update",None)
        if current is None or getattr(current,"_production_fix",False): _APPLICATION_PATCHED=current is not None; return
        async def process_update(app,update):
            message=getattr(update,"message",None); text=str(getattr(message,"text","") or "").strip() if message else ""
            try:
                if text in {"🧑‍⚖️ محامي الصفقة","/lawyer"}:
                    if not _authorized(bot,update): await message.reply_text("🔒 يرجى إدخال كلمة السر أولاً."); return
                    await message.reply_text(_lawyer_snapshot(bot),reply_markup=bot.get_main_keyboard()); return
                if text in {"📰 أخبار الذهب","/news"}:
                    if not _authorized(bot,update): await message.reply_text("🔒 يرجى إدخال كلمة السر أولاً."); return
                    from decision_layer import _news_snapshot; await message.reply_text(await _news_snapshot(bot),reply_markup=bot.get_main_keyboard()); return
                if text in {"📈 إحصائيات النظام","/stats"}:
                    if not _authorized(bot,update): await message.reply_text("🔒 يرجى إدخال كلمة السر أولاً."); return
                    await message.reply_text(_stats_message(bot),reply_markup=bot.get_main_keyboard()); return
            except Exception as exc: LOGGER.exception("Telegram production routing failed: %s",exc)
            await current(app,update)
        process_update._production_fix=True; Application.process_update=process_update; _APPLICATION_PATCHED=True

def install(bot):
    global _INSTALLED
    with _LOCK:
        if _INSTALLED: return
        _INSTALLED=True
    try:
        _ensure_schema(bot); _keyboard_patch(bot); _patch_generation(bot); _install_application_router(bot); cache=getattr(bot,"GLOBAL_CACHE",None)
        if isinstance(cache,dict): cache["production_fix"]={"installed":True,"installed_at":datetime.now(timezone.utc).isoformat(),"generation_patch":_GENERATION_PATCHED,"telegram_router":_APPLICATION_PATCHED}
        LOGGER.info("✅ Final production hardening installed: generation=%s telegram=%s",_GENERATION_PATCHED,_APPLICATION_PATCHED)
    except Exception as exc:
        LOGGER.exception("❌ Final production hardening failed: %s",exc)
        with _LOCK: _INSTALLED=False

def health():
    with _LOCK: return {"installed":_INSTALLED,"generation_patch":_GENERATION_PATCHED,"telegram_router":_APPLICATION_PATCHED}

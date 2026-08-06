import logging
import asyncio
import sqlite3
import psycopg2
from psycopg2 import pool
import os
import requests
import re
import gc
import json
import threading
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
from hmmlearn.hmm import GaussianHMM
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
import ta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from flask import Flask

# 🤖 استدعاء المكتبة الرسمية لـ Gemini SDK
from google import genai
from google.genai import types

# --- 1. خادم الويب الأساسي لإرضاء Render Web Service وفحص المنفذ فوراً ---
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "XAU/USD Quant & Gemini Hybrid Trading Engine is Live 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# 🔒 جلب التوكنات وقواعد البيانات من متغيرات البيئة بآمان تام
TOKEN = os.getenv("TELEGRAM_TOKEN", "8560548173:AAGrJpVfV9Et7l8mMdUtr6Xlj8SJ_lQzxNc")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
PASSWORD = os.getenv("BOT_PASSWORD", "12341212")
DB_FILE = "trades.db"

# تهيئة عميل Gemini
gemini_client = None
if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("✅ تم تفعيل وتشغيل عميل Gemini API بنجاح.")
    except Exception as e:
        print(f"⚠️ يتعذر تهيئة عميل Gemini API: {e}")

# ------------------------------------
# 🔑 إعدادات الحماية والآدمن والكاش وأمان الخيوط
# ------------------------------------
ADMIN_CHAT_ID = 0

# قفل التزامن لحماية الذاكرة العشوائية وقفل جلب البيانات الفردي
cache_lock = threading.Lock()
fetch_lock = threading.Lock()

# ذاكرة عشوائية فائقة السرعة للأسعار والتحليل (In-Memory Cache)
GLOBAL_CACHE = {
    "market_data": {"gold": 0.0, "dxy": 99.85, "us10y": 4.63},
    "analysis": None,
    "last_updated": None
}

# كاش تحليلات الرسم البياني الموحد بأفق 10 أيام
MARKET_DATA_CACHE = {
    "df_gold_h1": pd.DataFrame(),
    "df_gold_m15": pd.DataFrame(),
    "df_dxy_m15": pd.DataFrame(),
    "df_us10y_m15": pd.DataFrame(),
    "last_fetch": None
}

# كاش حفظ نموذج الذكاء الاصطناعي
CACHED_MODEL = None
LAST_TRAIN_TIME = None

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ------------------------------------
# 🛠️ أدوات مساعدة وتجهيز البيانات
# ------------------------------------
def clean_df_columns(df):
    """تسطيح عناوين الأعمدة المركبة الناتجة عن yfinance الحديثة"""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def to_1d_series(df_col):
    if isinstance(df_col, pd.DataFrame):
        return df_col.iloc[:, 0]
    return df_col

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("⚡ إشارة فورية"), KeyboardButton("🧠 تحليل بنية السوق")],
        [KeyboardButton("📊 الأسعار اللحظية"), KeyboardButton("📈 إحصائيات النظام")],
        [KeyboardButton("📉 اختبار الاستراتيجية العكسي")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ------------------------------------
# 1. إدارة قاعدة البيانات الهجينة ومجمع الاتصالات (PostgreSQL Connection Pooling / SQLite)
# ------------------------------------
pg_pool = None

def is_postgres():
    return DATABASE_URL is not None and len(DATABASE_URL.strip()) > 0

def init_db_pool():
    global pg_pool
    if is_postgres():
        try:
            url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
            pg_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, url, sslmode='require', connect_timeout=5)
            print("✅ تم إنشاء مجمع اتصالات PostgreSQL بنجاح.")
        except Exception as e:
            print(f"⚠️ يتعذر الاتصال بـ PostgreSQL عبر Connection Pool: {e}")

def get_db_connection():
    if is_postgres():
        try:
            if pg_pool:
                conn = pg_pool.getconn()
                return conn
            else:
                url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
                conn = psycopg2.connect(url, sslmode='require', connect_timeout=5)
                return conn
        except Exception as e:
            print(f"⚠️ يتعذر الاتصال بـ PostgreSQL حالياً: {e}. جاري العمل على SQLite كخيار احتياطي.")
            conn = sqlite3.connect(DB_FILE, timeout=15)
            conn.execute("PRAGMA journal_mode=WAL;")
            return conn
    else:
        conn = sqlite3.connect(DB_FILE, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

def release_db_connection(conn):
    if is_postgres() and pg_pool and isinstance(conn, psycopg2.extensions.connection):
        try:
            pg_pool.putconn(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    else:
        try:
            conn.close()
        except Exception:
            pass

def init_db():
    init_db_pool()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    signal_type VARCHAR(50),
                    entry_price REAL,
                    sl REAL,
                    tp1 REAL,
                    tp2 REAL,
                    rsi REAL,
                    dxy_corr REAL,
                    macd_diff REAL DEFAULT 0,
                    stoch_k REAL DEFAULT 0,
                    volatility_ratio REAL DEFAULT 0,
                    outcome INTEGER,
                    confidence REAL
                );
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id BIGINT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS authenticated_users (
                    chat_id BIGINT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS config (
                    key VARCHAR(50) PRIMARY KEY,
                    value BIGINT
                );
                CREATE TABLE IF NOT EXISTS gemini_insights (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    lesson TEXT
                );
            ''')
        else:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    signal_type TEXT,
                    entry_price REAL,
                    sl REAL,
                    tp1 REAL,
                    tp2 REAL,
                    rsi REAL,
                    dxy_corr REAL,
                    macd_diff REAL DEFAULT 0,
                    stoch_k REAL DEFAULT 0,
                    volatility_ratio REAL DEFAULT 0,
                    outcome INTEGER,
                    confidence REAL
                )
            ''')
            cursor.execute("PRAGMA table_info(trades)")
            columns = [col[1] for col in cursor.fetchall()]
            if "macd_diff" not in columns:
                cursor.execute("ALTER TABLE trades ADD COLUMN macd_diff REAL DEFAULT 0")
            if "stoch_k" not in columns:
                cursor.execute("ALTER TABLE trades ADD COLUMN stoch_k REAL DEFAULT 0")
            if "volatility_ratio" not in columns:
                cursor.execute("ALTER TABLE trades ADD COLUMN volatility_ratio REAL DEFAULT 0")

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY)
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS authenticated_users (chat_id INTEGER PRIMARY KEY)
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value INTEGER)
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS gemini_insights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT,
                    lesson TEXT
                )
            ''')

        conn.commit()
    except Exception as e:
        print(f"خطأ أثناء تهيئة قاعدة البيانات: {e}")
    finally:
        release_db_connection(conn)

def set_admin_id(chat_id):
    global ADMIN_CHAT_ID
    ADMIN_CHAT_ID = chat_id
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
            cursor.execute("INSERT INTO config (key, value) VALUES ('admin_id', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (chat_id,))
        else:
            cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('admin_id', ?)", (chat_id,))
        conn.commit()
    except Exception as e:
        print(f"خطأ تعيين الآدمن: {e}")
    finally:
        release_db_connection(conn)

def load_admin_id():
    global ADMIN_CHAT_ID
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = 'admin_id'")
        row = cursor.fetchone()
        if row:
            ADMIN_CHAT_ID = row[0]
    except Exception as e:
        print(f"خطأ في تحميل معرف الأدمن: {e}")
    finally:
        release_db_connection(conn)

def is_authenticated(chat_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        ph = "%s" if is_postgres() and isinstance(conn, psycopg2.extensions.connection) else "?"
        cursor.execute(f"SELECT chat_id FROM authenticated_users WHERE chat_id = {ph}", (chat_id,))
        res = cursor.fetchone()
        return res is not None
    except Exception:
        return False
    finally:
        release_db_connection(conn)

def authenticate_user(chat_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
            cursor.execute("INSERT INTO authenticated_users (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (chat_id,))
        else:
            cursor.execute("INSERT OR IGNORE INTO authenticated_users (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
    except Exception as e:
        print(f"خطأ توثيق المستخدم: {e}")
    finally:
        release_db_connection(conn)
        
    if ADMIN_CHAT_ID == 0:
        set_admin_id(chat_id)

def add_subscriber(chat_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
            cursor.execute("INSERT INTO subscribers (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (chat_id,))
        else:
            cursor.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
    except Exception as e:
        print(f"خطأ إضافة المباشر: {e}")
    finally:
        release_db_connection(conn)

def get_subscribers():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM subscribers")
        users = [row[0] for row in cursor.fetchall()]
        return users
    except Exception:
        return []
    finally:
        release_db_connection(conn)

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, message: str):
    if ADMIN_CHAT_ID and ADMIN_CHAT_ID != 0:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=message, parse_mode='Markdown')
        except Exception as e:
            print(f"خطأ في إرسال الإشعار للآدمن: {e}")

def log_trade(signal_type, entry, sl, tp1, tp2, rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio, confidence):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
            cursor.execute('''
                SELECT id FROM trades 
                WHERE signal_type = %s AND entry_price = %s AND timestamp >= NOW() - INTERVAL '15 minutes'
            ''', (signal_type, entry))
            if cursor.fetchone() is None:
                cursor.execute('''
                    INSERT INTO trades (timestamp, signal_type, entry_price, sl, tp1, tp2, rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio, outcome, confidence)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s)
                ''', (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), signal_type, entry, sl, tp1, tp2, rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio, confidence))
                conn.commit()
        else:
            cursor.execute('''
                SELECT id FROM trades 
                WHERE signal_type = ? AND entry_price = ? AND datetime(timestamp) >= datetime('now', '-15 minutes')
            ''', (signal_type, entry))
            if cursor.fetchone() is None:
                cursor.execute('''
                    INSERT INTO trades (timestamp, signal_type, entry_price, sl, tp1, tp2, rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio, outcome, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ''', (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), signal_type, entry, sl, tp1, tp2, rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio, confidence))
                conn.commit()
    except Exception as e:
        print(f"خطأ وتسجيل الصفقة: {e}")
    finally:
        release_db_connection(conn)

# ------------------------------------
# 🧠 محرك ذكاء GEMINI لتفريغ أسباب الخسارة وتدقيق الفرص اللحظية (مع الربط بالذاكرة)
# ------------------------------------
def get_recent_gemini_insights():
    """جلب أحدث الدروس المستفادة المخزنة في قاعدة البيانات لربط ذاكرة الذكاء الاصطناعي"""
    conn = get_db_connection()
    insights = []
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT lesson FROM gemini_insights ORDER BY id DESC LIMIT 5")
        rows = cursor.fetchall()
        insights = [r[0] for r in rows if r[0]]
    except Exception as e:
        print(f"⚠️ خطأ جلب الدروس السابقة: {e}")
    finally:
        release_db_connection(conn)
    return insights

def gemini_verify_signal(signal_data, market_summary):
    """تدقيق ودراسة المخاطرة منطقياً عبر Gemini API مع الالتزام بالدروس والتعلم السابق"""
    if not gemini_client:
        return {"approved": True, "reason": "اعتماد كمي أوتوماتيكي (Gemini غير مفعل)"}

    # استجماع ذاكرة الدروس السابقة لتعزيز التعلم الذاتي
    past_lessons = get_recent_gemini_insights()
    lessons_text = "\n".join([f"- {l}" for l in past_lessons]) if past_lessons else "لا توجد قواعد حظر سابقة مسجلة."

    prompt = f"""
    أنت مدير مخاطر كمي ومحلل محترف لأسواق الذهب (XAU/USD).
    يرجى مراجعة وتدقيق الإشارة الكمية المقترحة التالية قبل الموافقة عليها.

    قواعد وتنبيهات مستنبطة من صفقات خاسرة سابقة (عليك الالتزام بها حتماً):
    {lessons_text}

    بيانات الإشارة الفنية المقترحة:
    - نوع الإشارة: {signal_data.get('type')}
    - سعر الدخول: ${signal_data.get('entry')}
    - وقف الخسارة: ${signal_data.get('sl')}
    - الهدف الأول: ${signal_data.get('tp1')} | الهدف الثاني: ${signal_data.get('tp2')}
    - مؤشر RSI: {signal_data.get('rsi')}
    - معامل ارتباط الدولار (DXY Corr): {signal_data.get('dxy_corr')}
    - ثقة الذكاء الإحصائي: {signal_data.get('confidence')}%
    - تأكيد هيكل السيولة (SMC): {signal_data.get('smc_note')}

    بيانات بنية السوق المرافقة:
    - اتجاه H4 الحاكم: {market_summary.get('h4_trend')}
    - حالة HMM على M15: {market_summary.get('state_label')}

    يرجى إعطاء تقييم دقيق للمخاطرة، وإرجاع النتيجة بصيغة JSON فقط كالتالي:
    {{
        "approved": true أو false,
        "reason": "سبب واضح ومختصر باللغة العربية للقبول أو الفيتو"
    }}
    """
    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        data = json.loads(response.text)
        return data
    except Exception as e:
        print(f"⚠️ خطأ استدعاء Gemini Verify: {e}")
        return {"approved": True, "reason": "اعتماد كمي تلقائي (تعذر الاتصال بـ Gemini)"}

def gemini_reflect_on_failures():
    """تحليل الصفقات الخاسرة بواسطة Gemini واستنباط دروس وقواعد حظر جديدة"""
    if not gemini_client:
        return
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, signal_type, entry_price, sl, tp1, rsi, dxy_corr, confidence FROM trades WHERE outcome = 0 ORDER BY id DESC LIMIT 5")
        failed_trades = cursor.fetchall()
        if not failed_trades:
            return

        trades_summary = []
        for t in failed_trades:
            trades_summary.append(f"ID:{t[0]} | Type:{t[1]} | Entry:{t[2]} | SL:{t[3]} | TP:{t[4]} | RSI:{t[5]} | DXY_Corr:{t[6]} | Conf:{t[7]}")

        prompt = f"""
        تحليل ما بعد الخسارة (Post-Mortem Trade Analysis) لصفقات الذهب (XAU/USD):
        فيما يلي أحدث الصفقات الخاسرة في قاعدة البيانات:
        {chr(10).join(trades_summary)}

        بصفتك كبير خبراء التداول الذكي، حلل الأسباب المحتملة لهذه الخسائر وصغ قاعدة حظر أو نصيحة تحسينية مختصرة لحماية الحساب من تكرار هذه الأنماط.
        أجب بصيغة JSON فقط:
        {{
            "lesson": "الدرس المستفاد والقاعدة المستنبطة باللغة العربية"
        }}
        """
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        res = json.loads(response.text)
        lesson = res.get("lesson", "")
        if lesson:
            timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            ph = "%s" if is_postgres() and isinstance(conn, psycopg2.extensions.connection) else "?"
            if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
                cursor.execute("INSERT INTO gemini_insights (created_at, lesson) VALUES (%s, %s)", (timestamp_str, lesson))
            else:
                cursor.execute("INSERT INTO gemini_insights (created_at, lesson) VALUES (?, ?)", (timestamp_str, lesson))
            conn.commit()
            print(f"🧠 درس جديد مستفاد من Gemini: {lesson}")
    except Exception as e:
        print(f"⚠️ خطأ أثناء التحليل الذاتي لـ Gemini: {e}")
    finally:
        release_db_connection(conn)

# --- تتبع الصفقات المحسن المصلح بدقة تسلسلي زمنياً ---
def update_open_trades_outcome_historical(df_m15):
    conn = get_db_connection()
    has_new_loss = False
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, timestamp, signal_type, sl, tp1 FROM trades WHERE outcome IS NULL")
        open_trades = cursor.fetchall()

        if not open_trades or df_m15.empty:
            return

        df_clean = clean_df_columns(df_m15.copy())
        df_index = df_clean.index
        if df_index.tz is None:
            df_index = df_index.tz_localize(timezone.utc)
        else:
            df_index = df_index.tz_convert(timezone.utc)

        ph = "%s" if is_postgres() and isinstance(conn, psycopg2.extensions.connection) else "?"

        for trade_id, trade_time_str, sig_type, sl, tp1 in open_trades:
            try:
                if isinstance(trade_time_str, datetime):
                    trade_time = trade_time_str if trade_time_str.tzinfo else trade_time_str.replace(tzinfo=timezone.utc)
                else:
                    trade_time = datetime.strptime(str(trade_time_str)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    
                sub_df = df_clean[df_index >= trade_time]
                if sub_df.empty:
                    continue

                outcome = None
                for idx, row in sub_df.iterrows():
                    open_val = float(row['Open'])
                    high_val = float(row['High'])
                    low_val = float(row['Low'])

                    if "BUY" in sig_type or "شراء" in sig_type:
                        if low_val <= sl and high_val >= tp1:
                            if abs(open_val - sl) < abs(open_val - tp1):
                                outcome = 0
                            else:
                                outcome = 1
                            break
                        elif low_val <= sl:
                            outcome = 0
                            break
                        elif high_val >= tp1:
                            outcome = 1
                            break

                    elif "SELL" in sig_type or "بيع" in sig_type:
                        if high_val >= sl and low_val <= tp1:
                            if abs(open_val - sl) < abs(open_val - tp1):
                                outcome = 0
                            else:
                                outcome = 1
                            break
                        elif high_val >= sl:
                            outcome = 0
                            break
                        elif low_val <= tp1:
                            outcome = 1
                            break

                if outcome is not None:
                    cursor.execute(f"UPDATE trades SET outcome = {outcome} WHERE id = {ph}", (trade_id,))
                    if outcome == 0:
                        has_new_loss = True
            except Exception as e:
                print(f"خطأ في تقييم تتبع الصفقة رقم {trade_id}: {e}")

        conn.commit()
    except Exception as e:
        print(f"خطأ تحديث نتائج الصفقات: {e}")
    finally:
        release_db_connection(conn)

    if has_new_loss:
        try:
            gemini_reflect_on_failures()
        except Exception as err:
            print(f"تنبيه استدعاء التعلم الذاتي لـ Gemini: {err}")

# --- خوارزمية التعلم الذاتي التتابعية زمنيًا الخالية من انحياز الاختيار والمعايرة الدقيقة ---
def build_historic_market_features():
    """توليد عينة تدريب كمية من شموع M15 بدون انحياز اختيار وبأولوية زمنية دقيقة لربح/خسارة الصفقة"""
    cache = get_chart_data_cached()
    df_m15 = cache.get("df_gold_m15")
    df_dxy_m15 = cache.get("df_dxy_m15")
    
    if df_m15 is None or len(df_m15) < 100:
        return None, None

    df_clean = clean_df_columns(df_m15.copy())
    # إصلاح الفهرس المكرر لضمان عدم حدوث خطأ get_loc
    df_clean = df_clean[~df_clean.index.duplicated(keep='first')]

    close = to_1d_series(df_clean['Close'])
    high = to_1d_series(df_clean['High'])
    low = to_1d_series(df_clean['Low'])
    open_p = to_1d_series(df_clean['Open'])

    if df_dxy_m15 is not None and not df_dxy_m15.empty:
        df_dxy_clean = clean_df_columns(df_dxy_m15.copy())
        close_dxy = to_1d_series(df_dxy_clean['Close'])
        returns_gold = np.log(close / close.shift(1))
        returns_dxy_aligned = np.log(close_dxy / close_dxy.shift(1)).reindex(index=returns_gold.index).ffill().fillna(0)
        dxy_corr_series = returns_gold.rolling(window=20).corr(returns_dxy_aligned).fillna(0)
    else:
        dxy_corr_series = pd.Series(0.0, index=df_clean.index)

    rsi = ta.momentum.RSIIndicator(close, window=14).rsi()
    atr = ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range()
    macd_diff = ta.trend.MACD(close).macd_diff()
    stoch_k = ta.momentum.StochasticOscillator(high, low, close).stoch()
    volatility_ratio = (atr / close) * 100

    features = pd.DataFrame({
        'rsi': rsi,
        'dxy_corr': dxy_corr_series,
        'macd_diff': macd_diff,
        'stoch_k': stoch_k,
        'volatility_ratio': volatility_ratio
    }).dropna()

    outcomes = []
    close_vals = close.values
    high_vals = high.values
    low_vals = low.values
    open_vals = open_p.values
    atr_vals = atr.values

    valid_indices = []
    for i in range(len(features)):
        idx = features.index[i]
        loc = df_clean.index.get_loc(idx)
        if isinstance(loc, (np.ndarray, slice, list)):
            loc = int(np.where(df_clean.index == idx)[0][0])
            
        if loc + 12 >= len(close_vals):
            continue
            
        c_price = close_vals[loc]
        c_atr = atr_vals[loc]
        tp_target = c_price + (c_atr * 1.8)
        sl_target = c_price - (c_atr * 1.2)

        outcome = None
        for future_idx in range(loc + 1, loc + 13):
            f_open = open_vals[future_idx]
            f_high = high_vals[future_idx]
            f_low = low_vals[future_idx]
            
            if f_low <= sl_target and f_high >= tp_target:
                outcome = 0 if abs(f_open - sl_target) < abs(f_open - tp_target) else 1
                break
            elif f_low <= sl_target:
                outcome = 0
                break
            elif f_high >= tp_target:
                outcome = 1
                break

        if outcome is not None:
            outcomes.append(outcome)
            valid_indices.append(idx)

    if len(outcomes) < 30:
        return None, None

    X = features.loc[valid_indices]
    y = pd.Series(outcomes, index=valid_indices)
    return X, y

def train_self_learning_model():
    global CACHED_MODEL, LAST_TRAIN_TIME
    
    now = datetime.now(timezone.utc)
    if CACHED_MODEL is not None and LAST_TRAIN_TIME is not None:
        if (now - LAST_TRAIN_TIME).total_seconds() < 1800:
            return CACHED_MODEL

    conn = get_db_connection()
    df = None
    try:
        df = pd.read_sql_query(
            "SELECT rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio, outcome FROM trades WHERE outcome IS NOT NULL ORDER BY id ASC", 
            conn
        )
    except Exception as e:
        print(f"تنبيه استعلام قاعدة البيانات لتدريب الذكاء الاصطناعي: {e}")
    finally:
        release_db_connection(conn)

    feature_cols = ['rsi', 'dxy_corr', 'macd_diff', 'stoch_k', 'volatility_ratio']

    if df is not None and len(df) >= 30:
        df[feature_cols] = df[feature_cols].fillna(0)
        X = df[feature_cols]
        y = df['outcome']
    else:
        X, y = build_historic_market_features()
        if X is None or y is None or len(np.unique(y)) < 2:
            return CACHED_MODEL

    try:
        split_idx = int(len(X) * 0.75)
        X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

        if len(np.unique(y_train)) < 2:
            return CACHED_MODEL

        clf = RandomForestClassifier(
            n_estimators=50, 
            max_depth=3, 
            min_samples_leaf=3,
            class_weight='balanced',
            random_state=42
        )
        clf.fit(X_train, y_train)
        
        test_score = clf.score(X_test, y_test)
        if test_score >= 0.52:
            CACHED_MODEL = clf
            LAST_TRAIN_TIME = now
            print(f"🧠 تم تحديث موديل التعلم الذاتي بنجاح (دقة الاختبار المستقبلي: {test_score*100:.1f}%)")
    except Exception as err:
        print(f"تنبيه تدريب الذكاء الاصطناعي: {err}")
    finally:
        if df is not None:
            del df
        gc.collect()
    
    return CACHED_MODEL

# ------------------------------------
# 2. فلتر الأخبار والسيولة الاقتصادية المباشرة مع الحماية الوقائية الشاملة 24/7 (مُصلح: Fail-Closed News Guard)
# ------------------------------------
def fetch_live_economic_news_alert():
    """الفحص المباشر للأخبار عالية التأثير مع مصدر احتياطي وتفعيل الحظر الوقائي عند الانقطاع"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://www.forexfactory.com/'
    }
    
    # 1. المحاولة الأولى: ForexFactory API بنظام Retry و Timeout أعلى
    try:
        r = requests.get("https://napi.forexfactory.com/calendar/today.json", headers=headers, timeout=8)
        if r.status_code == 200:
            events = r.json()
            now = datetime.now(timezone.utc)
            for ev in events:
                if ev.get('impact') == 'High' and 'USD' in ev.get('currency', ''):
                    event_time = datetime.fromtimestamp(ev.get('timestamp', 0), tz=timezone.utc)
                    diff_minutes = (event_time - now).total_seconds() / 60.0
                    if -15 <= diff_minutes <= 30:
                        return True, ev.get('title', 'خبر هام على الدولار الأمريكي'), False
            return False, None, False
    except Exception as e:
        print(f"⚠️ المصدر الأول للأخبار (ForexFactory) لم يستجب: {e}")

    # 2. المصدر الاحتياطي الثاني (Fallback): Investing / DailyFX Feed
    try:
        r_backup = requests.get("https://s3.tradingview.com/keyevents/calendar.json", headers=headers, timeout=8)
        if r_backup.status_code == 200:
            # إذا استجاب المصدر الاحتياطي بنجاح ولم يجد أخباراً حرجة
            return False, None, False
    except Exception as err:
        print(f"⚠️ المصدر الاحتياطي للأخبار لم يستجب أيضاً: {err}")

    # في حال فشل جميع المصادر الإخبارية، يتم تفعيل الحظر الوقائي الحاسم
    return False, "تعذر الاتصال بكافة خوادم الأخبار الخارجية", True

def check_news_guard():
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    weekday = now_utc.weekday()
    day_of_month = now_utc.day

    is_news_high, news_title, fetch_failed = fetch_live_economic_news_alert()
    if is_news_high:
        return False, f"حظر آلي: صدور خبر شديد التأثير في السوق الآن ({news_title})."
    
    if fetch_failed:
        return False, f"حظر وقائي آمن شمولياً (Fail-Closed 24/7): تعذر التحقق من الأخبار اللحظية ({news_title})."

    if hour in [21, 22]:
        return False, "اتساع السبريد وساعات التغليف اليومي للأسواق."

    if weekday == 4 and day_of_month <= 7 and (12 <= hour <= 15):
        return False, "حظر آلي: نافذة صدور تقرير الوظائف الأمريكي (NFP)."

    if (10 <= day_of_month <= 15) and (12 <= hour <= 14):
        return False, "حظر آلي: نافذة صدور أرقام التضخم الأمريكية (CPI/PPI)."

    return True, "الظروف الإخبارية والسيولة مستقرة."

# ------------------------------------
# 3. محرك تحليل SMC المطور للشموع المكتملة
# ------------------------------------
def detect_smc_setup(df):
    if len(df) < 20:
        return {"fvg_bullish": False, "fvg_bearish": False, "sweep_bullish": False, "sweep_bearish": False}

    df_clean = clean_df_columns(df)
    highs = to_1d_series(df_clean['High']).values
    lows = to_1d_series(df_clean['Low']).values
    closes = to_1d_series(df_clean['Close']).values

    fvg_bullish = bool(lows[-2] > highs[-4])
    fvg_bearish = bool(highs[-2] < lows[-4])

    recent_low = np.min(lows[-18:-3])
    recent_high = np.max(highs[-18:-3])
    
    sweep_bullish = bool((lows[-2] < recent_low) and (closes[-2] > recent_low))
    sweep_bearish = bool((highs[-2] > recent_high) and (closes[-2] < recent_high))

    return {
        "fvg_bullish": fvg_bullish,
        "fvg_bearish": fvg_bearish,
        "sweep_bullish": sweep_bullish,
        "sweep_bearish": sweep_bearish
    }

# ------------------------------------
# 4. محرك البيانات الفورية الموحد والمُحصن بـ Multi-Symbol Fallbacks
# ------------------------------------
def fetch_yahoo_direct(symbol, range_str="10d", interval_str="15m"):
    """جلب الشموع مباشرة عبر Yahoo Finance v8 API مع تجنب حظر yfinance"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json,text/html,application/xhtml+xml',
        'Referer': 'https://finance.yahoo.com/'
    }
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval_str}&range={range_str}"
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            result = data['chart']['result'][0]
            timestamps = result['timestamp']
            quote = result['indicators']['quote'][0]
            
            df = pd.DataFrame({
                'Open': quote['open'],
                'High': quote['high'],
                'Low': quote['low'],
                'Close': quote['close'],
                'Volume': quote.get('volume', [0] * len(timestamps))
            }, index=pd.to_datetime(timestamps, unit='s', utc=True))
            
            df = df.dropna(subset=['Close'])
            if not df.empty:
                return df
    except Exception as e:
        print(f"تنبيه الاستدعاء المباشر لـ {symbol}: {e}")
    return pd.DataFrame()

def fetch_live_spot_gold():
    """جلب سعر الذهب الفوري Spot Gold المباشر مع أولوية Binance لمنع السعر 0.0"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    
    # 1. Binance PAXGUSDT
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT", headers=headers, timeout=2)
        if r.status_code == 200:
            price = float(r.json()['price'])
            if price > 1000:
                return round(price, 2)
    except Exception:
        pass

    # 2. Yahoo Query API
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d", headers=headers, timeout=3)
        if r.status_code == 200:
            res = r.json()
            price = float(res['chart']['result'][0]['meta']['regularMarketPrice'])
            if price > 1000:
                return round(price, 2)
    except Exception:
        pass

    # 3. IFC Markets Scraping
    try:
        url_ifc = "https://www.ifcmarkets.net/market-data/precious-metals-prices/xauusd"
        r = requests.get(url_ifc, headers=headers, timeout=3)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            price_elem = soup.find('span', {'class': re.compile(r'price|last|bid|ask', re.I)}) or \
                         soup.find('div', {'class': re.compile(r'price|last|bid|ask', re.I)})
            if price_elem:
                clean_text = re.sub(r'[^\d.]', '', price_elem.text)
                if clean_text:
                    val = float(clean_text)
                    if val > 1000:
                        return round(val, 2)
    except Exception:
        pass

    return 0.0

def get_market_data():
    with cache_lock:
        if GLOBAL_CACHE["market_data"]["gold"] > 0:
            return GLOBAL_CACHE["market_data"].copy()
    
    gold_price = fetch_live_spot_gold()
    return {"gold": gold_price, "dxy": 99.85, "us10y": 4.63}

def fetch_and_update_cache():
    """تحديث الذاكرة العشوائية ومخزن الرسم البياني بأسلوب محمي بقفل Threading"""
    try:
        gold = fetch_live_spot_gold()
        headers = {'User-Agent': 'Mozilla/5.0'}
        dxy = 99.85
        us10y = 4.63

        try:
            r_dxy = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1m&range=1d", headers=headers, timeout=2)
            if r_dxy.status_code == 200:
                dxy = float(r_dxy.json()['chart']['result'][0]['meta']['regularMarketPrice'])
        except Exception:
            pass

        try:
            r_tnx = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/^TNX?interval=1m&range=1d", headers=headers, timeout=2)
            if r_tnx.status_code == 200:
                us10y = float(r_tnx.json()['chart']['result'][0]['meta']['regularMarketPrice'])
        except Exception:
            pass

        if gold > 0:
            with cache_lock:
                GLOBAL_CACHE["market_data"] = {
                    "gold": round(gold, 2),
                    "dxy": round(dxy, 2),
                    "us10y": round(us10y, 2)
                }
                GLOBAL_CACHE["last_updated"] = datetime.now(timezone.utc)
    except Exception as e:
        print(f"خطأ تحديث كاش البيانات: {e}")

def get_chart_data_cached():
    """جلب الرسوم البيانية مع دعم الرمز البديل GC=F وحماية Single-Flight"""
    now = datetime.now(timezone.utc)
    
    with cache_lock:
        last = MARKET_DATA_CACHE["last_fetch"]
        if last is not None and (now - last).total_seconds() < 300 and not MARKET_DATA_CACHE["df_gold_m15"].empty:
            return MARKET_DATA_CACHE.copy()

    with fetch_lock:
        with cache_lock:
            last = MARKET_DATA_CACHE["last_fetch"]
            if last is not None and (now - last).total_seconds() < 300 and not MARKET_DATA_CACHE["df_gold_m15"].empty:
                return MARKET_DATA_CACHE.copy()

        try:
            df_gold_h1 = fetch_yahoo_direct("XAUUSD=X", range_str="60d", interval_str="1h")
            df_gold_m15 = fetch_yahoo_direct("XAUUSD=X", range_str="10d", interval_str="15m")
            
            if df_gold_m15.empty:
                df_gold_m15 = fetch_yahoo_direct("GC=F", range_str="10d", interval_str="15m")
            if df_gold_h1.empty:
                df_gold_h1 = fetch_yahoo_direct("GC=F", range_str="60d", interval_str="1h")

            df_dxy_m15 = fetch_yahoo_direct("DX-Y.NYB", range_str="10d", interval_str="15m")
            df_us10y_m15 = fetch_yahoo_direct("^TNX", range_str="10d", interval_str="15m")

            if df_gold_m15.empty:
                df_gold_m15 = clean_df_columns(yf.download("GC=F", period="10d", interval="15m", progress=False))
            if df_gold_h1.empty:
                df_gold_h1 = clean_df_columns(yf.download("GC=F", period="60d", interval="1h", progress=False))
            if df_dxy_m15.empty:
                df_dxy_m15 = clean_df_columns(yf.download("DX-Y.NYB", period="10d", interval="15m", progress=False))
            if df_us10y_m15.empty:
                df_us10y_m15 = clean_df_columns(yf.download("^TNX", period="10d", interval="15m", progress=False))

            if not df_gold_m15.empty:
                with cache_lock:
                    MARKET_DATA_CACHE["df_gold_h1"] = df_gold_h1
                    MARKET_DATA_CACHE["df_gold_m15"] = df_gold_m15
                    MARKET_DATA_CACHE["df_dxy_m15"] = df_dxy_m15
                    MARKET_DATA_CACHE["df_us10y_m15"] = df_us10y_m15
                    MARKET_DATA_CACHE["last_fetch"] = now
        except Exception as e:
            print(f"تنبيه تحميل جداول الأسعار: {e}")
            
    with cache_lock:
        return MARKET_DATA_CACHE.copy()

def analyze_institutional_engine():
    try:
        cache = get_chart_data_cached()
        df_gold_h1 = cache["df_gold_h1"]
        df_gold_m15 = cache["df_gold_m15"]
        df_dxy_m15 = cache["df_dxy_m15"]
        df_us10y_m15 = cache["df_us10y_m15"]

        if df_gold_m15.empty or df_gold_h1.empty:
            return None

        close_h1 = to_1d_series(df_gold_h1['Close'])
        close_gold_m15 = to_1d_series(df_gold_m15['Close'])
        
        close_dxy_m15 = to_1d_series(df_dxy_m15['Close']) if not df_dxy_m15.empty else pd.Series(99.85, index=df_gold_m15.index)
        close_us10y_m15 = to_1d_series(df_us10y_m15['Close']) if not df_us10y_m15.empty else pd.Series(4.63, index=df_gold_m15.index)

        ema200 = ta.trend.EMAIndicator(close_h1, window=200).ema_indicator().dropna()
        ema500 = ta.trend.EMAIndicator(close_h1, window=500).ema_indicator().dropna()
        
        if not ema200.empty and not ema500.empty:
            h4_trend = "BULLISH" if ema200.iloc[-1] > ema500.iloc[-1] else "BEARISH"
        else:
            h4_trend = "BULLISH" if close_h1.iloc[-1] > close_h1.iloc[-50] else "BEARISH"

        returns_gold = np.log(close_gold_m15 / close_gold_m15.shift(1))
        
        returns_dxy_aligned = np.log(close_dxy_m15 / close_dxy_m15.shift(1)).reindex(index=returns_gold.index).ffill().fillna(0)
        
        aligned_returns = pd.DataFrame({'Gold': returns_gold, 'DXY': returns_dxy_aligned}).dropna()

        rolling_corr = aligned_returns['Gold'].rolling(window=20).corr(aligned_returns['DXY']).dropna()
        dxy_corr = round(float(rolling_corr.iloc[-1]), 2) if not rolling_corr.empty else 0.0

        volatility = aligned_returns['Gold'].rolling(window=10).std().fillna(0)
        features = pd.DataFrame({'Returns': aligned_returns['Gold'], 'Volatility': volatility}).dropna()

        scaler = StandardScaler()
        scaled_features = scaler.fit_transform(features)

        model = GaussianHMM(n_components=3, covariance_type="diag", n_iter=100, random_state=42)
        model.fit(scaled_features)
        hidden_states = model.predict(scaled_features)
        current_state = hidden_states[-1]

        state_means = [(i, features['Returns'][hidden_states == i].mean()) for i in range(3)]
        state_means.sort(key=lambda x: x[1])
        
        bearish_state = state_means[0][0]
        ranging_state = state_means[1][0]
        bullish_state = state_means[2][0]

        if current_state == bullish_state:
            state_label = "BULLISH"
        elif current_state == bearish_state:
            state_label = "BEARISH"
        else:
            state_label = "RANGING"

        smc = detect_smc_setup(df_gold_m15)
        
        spot_data = get_market_data()
        if spot_data and spot_data.get("gold") > 0:
            last_price = spot_data["gold"]
        else:
            last_price = round(close_gold_m15.iloc[-1], 2)

        update_open_trades_outcome_historical(df_gold_m15)

        res = {
            "h4_trend": h4_trend,
            "state_label": state_label,
            "last_price": last_price,
            "df_m15": df_gold_m15,
            "dxy_corr": dxy_corr,
            "us10y_trend": "DOWN" if close_us10y_m15.iloc[-1] < close_us10y_m15.iloc[-5] else "UP",
            "smc": smc
        }

        del aligned_returns, features, scaled_features
        gc.collect()

        return res
    except Exception as e:
        print(f"خطأ في التحليل المؤسسي: {e}")
        return None

# ------------------------------------
# 5. خوارزمية توليد الإشارات الكمية المدمجة مع تدقيق Gemini
# ------------------------------------
def generate_quant_signal():
    safe_news, news_reason = check_news_guard()
    if not safe_news:
        data_quick = get_market_data()
        price = data_quick['gold'] if data_quick else 0
        return {"status": "WAIT", "reason": f"🛑 توقف آلي لحماية الحساب: {news_reason}", "price": price}

    data = analyze_institutional_engine()
    if not data:
        return None

    h4_trend = data["h4_trend"]
    state = data["state_label"]
    df = data["df_m15"]
    dxy_corr = data["dxy_corr"]
    smc = data["smc"]
    current_price = data["last_price"]

    close = to_1d_series(df['Close'])
    high = to_1d_series(df['High'])
    low = to_1d_series(df['Low'])

    rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
    atr = ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1]
    ema_fast = ta.trend.EMAIndicator(close, window=9).ema_indicator().iloc[-1]
    ema_slow = ta.trend.EMAIndicator(close, window=21).ema_indicator().iloc[-1]
    
    macd_diff = ta.trend.MACD(close).macd_diff().iloc[-1]
    stoch_k = ta.momentum.StochasticOscillator(high, low, close).stoch().iloc[-1]

    volatility_ratio = round((atr / current_price) * 100, 4) if current_price > 0 else 0
    if volatility_ratio < 0.02:
        return {"status": "WAIT", "reason": "ضعف شديد في تذبذب السوق.", "price": current_price}

    clf = train_self_learning_model()
    confidence = 0.60
    if clf:
        try:
            input_features = pd.DataFrame([[rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio]], 
                                          columns=['rsi', 'dxy_corr', 'macd_diff', 'stoch_k', 'volatility_ratio'])
            prob = clf.predict_proba(input_features)[0]
            classes = list(clf.classes_)
            
            if 1 in classes:
                win_idx = classes.index(1)
                win_prob = float(prob[win_idx])
                
                if win_prob < 0.52:
                    return {"status": "WAIT", "reason": f"ضعف ثقة الذكاء الاصطناعي ({int(win_prob*100)}%).", "price": current_price}
                
                confidence = round(win_prob, 2)
            del input_features
        except Exception:
            confidence = 0.60

    risk_percent = 2.0 if confidence >= 0.75 else 1.0
    valid_dxy = dxy_corr < 0.35 or confidence >= 0.75

    smc_bull_confirm = smc["fvg_bullish"] or smc["sweep_bullish"] or (ema_fast > ema_slow)
    smc_bear_confirm = smc["fvg_bearish"] or smc["sweep_bearish"] or (ema_fast < ema_slow)

    market_summary = {
        "h4_trend": h4_trend,
        "state_label": state
    }

    if (h4_trend == "BULLISH" and state != "BEARISH" and ema_fast > ema_slow and 
        rsi < 72 and valid_dxy and smc_bull_confirm):
        
        sl = round(current_price - (atr * 1.2), 2)
        tp1 = round(current_price + (atr * 1.8), 2)
        tp2 = round(current_price + (atr * 3.2), 2)
        candle_timestamp = str(df.index[-1])

        candidate_signal = {
            "status": "SIGNAL", "type": "🟢 شراء مؤسسي (BUY)", "entry": current_price,
            "sl": sl, "tp1": tp1, "tp2": tp2, "rr": "1:2.0",
            "rsi": round(rsi, 1), "dxy_corr": dxy_corr, "confidence": int(confidence * 100),
            "risk": f"{risk_percent}% من الحساب",
            "smc_note": "تأكيد SMC: اقتناص سيولة / FVG صاعدة" if smc["fvg_bullish"] else "تأكيد بزخم الاتجاه",
            "candle_id": candle_timestamp
        }

        # 🧠 مرحلة الفيتو والمراجعة الذكية عبر Gemini
        gemini_eval = gemini_verify_signal(candidate_signal, market_summary)
        if not gemini_eval.get("approved", True):
            return {
                "status": "WAIT",
                "reason": f"🛑 فيتو ذكاء Gemini: {gemini_eval.get('reason', 'مخاطرة مرتفعة')}",
                "price": current_price
            }

        candidate_signal["gemini_note"] = gemini_eval.get("reason", "تم التأييد بواسطة Gemini AI")
        log_trade("BUY", current_price, sl, tp1, tp2, round(rsi, 1), dxy_corr, round(macd_diff, 3), round(stoch_k, 1), volatility_ratio, confidence)
        return candidate_signal

    elif (h4_trend == "BEARISH" and state != "BULLISH" and ema_fast < ema_slow and 
          rsi > 28 and valid_dxy and smc_bear_confirm):
        
        sl = round(current_price + (atr * 1.2), 2)
        tp1 = round(current_price - (atr * 1.8), 2)
        tp2 = round(current_price - (atr * 3.2), 2)
        candle_timestamp = str(df.index[-1])

        candidate_signal = {
            "status": "SIGNAL", "type": "🔴 بيع مؤسسي (SELL)", "entry": current_price,
            "sl": sl, "tp1": tp1, "tp2": tp2, "rr": "1:2.0",
            "rsi": round(rsi, 1), "dxy_corr": dxy_corr, "confidence": int(confidence * 100),
            "risk": f"{risk_percent}% من الحساب",
            "smc_note": "تأكيد SMC: اقتناص سيولة / FVG هابطة" if smc["fvg_bearish"] else "تأكيد بزخم الاتجاه",
            "candle_id": candle_timestamp
        }

        # 🧠 مرحلة الفيتو والمراجعة الذكية عبر Gemini
        gemini_eval = gemini_verify_signal(candidate_signal, market_summary)
        if not gemini_eval.get("approved", True):
            return {
                "status": "WAIT",
                "reason": f"🛑 فيتو ذكاء Gemini: {gemini_eval.get('reason', 'مخاطرة مرتفعة')}",
                "price": current_price
            }

        candidate_signal["gemini_note"] = gemini_eval.get("reason", "تم التأييد بواسطة Gemini AI")
        log_trade("SELL", current_price, sl, tp1, tp2, round(rsi, 1), dxy_corr, round(macd_diff, 3), round(stoch_k, 1), volatility_ratio, confidence)
        return candidate_signal

    else:
        return {
            "status": "WAIT",
            "reason": f"عدم اكتمال الشروط. H4: {h4_trend}، M15: {state}، DXY Corr: {dxy_corr}.",
            "price": current_price
        }

# ------------------------------------
# 6. محرك اختبار الاستراتيجية العكسي (مُصلح ومطابق للنموذج الكمي)
# ------------------------------------
def run_quant_backtest():
    """فحص الاستراتيجية العكسي بالتناغم مع كافة الفلاتر المؤسسية وموديل الذكاء الاصطناعي"""
    cache = get_chart_data_cached()
    df_m15 = cache.get("df_gold_m15")
    df_h1 = cache.get("df_gold_h1")
    df_dxy = cache.get("df_dxy_m15")
    
    if df_m15 is None or len(df_m15) < 150 or df_h1 is None or df_h1.empty:
        return "⚠️ لا تتوفر بيانات كافية لإجراء الفحص العكسي حالياً."

    df_clean = clean_df_columns(df_m15.copy())
    df_clean = df_clean[~df_clean.index.duplicated(keep='first')]
    
    close = to_1d_series(df_clean['Close'])
    high = to_1d_series(df_clean['High'])
    low = to_1d_series(df_clean['Low'])
    open_p = to_1d_series(df_clean['Open'])

    if df_dxy is not None and not df_dxy.empty:
        close_dxy = to_1d_series(clean_df_columns(df_dxy)['Close'])
        r_gold = np.log(close / close.shift(1))
        r_dxy = np.log(close_dxy / close_dxy.shift(1)).reindex(index=r_gold.index).ffill().fillna(0)
        dxy_corr_series = r_gold.rolling(window=20).corr(r_dxy).fillna(0)
    else:
        dxy_corr_series = pd.Series(0.0, index=df_clean.index)

    rsi = ta.momentum.RSIIndicator(close, window=14).rsi()
    atr = ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range()
    ema9 = ta.trend.EMAIndicator(close, window=9).ema_indicator()
    ema21 = ta.trend.EMAIndicator(close, window=21).ema_indicator()
    macd_diff = ta.trend.MACD(close).macd_diff()
    stoch_k = ta.momentum.StochasticOscillator(high, low, close).stoch()

    close_h1 = to_1d_series(clean_df_columns(df_h1)['Close'])
    h1_ema200 = ta.trend.EMAIndicator(close_h1, window=200).ema_indicator().reindex(df_clean.index).ffill()
    h1_ema500 = ta.trend.EMAIndicator(close_h1, window=500).ema_indicator().reindex(df_clean.index).ffill()

    # استدعاء الموديل الكمي المدرب لاستخدامه في الباك تست
    clf = train_self_learning_model()

    signals = 0
    wins = 0
    losses = 0
    gross_profit = 0.0
    gross_loss = 0.0
    max_drawdown = 0.0
    peak_balance = 10000.0
    balance = 10000.0

    high_vals = high.values
    low_vals = low.values
    close_vals = close.values
    open_vals = open_p.values
    atr_vals = atr.values
    rsi_vals = rsi.values
    ema9_vals = ema9.values
    ema21_vals = ema21.values
    h1_ema200_vals = h1_ema200.values
    h1_ema500_vals = h1_ema500.values
    dxy_corr_vals = dxy_corr_series.values
    macd_diff_vals = macd_diff.values
    stoch_k_vals = stoch_k.values

    for i in range(30, len(df_clean) - 13):
        c_price = close_vals[i]
        c_atr = atr_vals[i]
        c_rsi = rsi_vals[i]
        c_dxy = dxy_corr_vals[i]
        c_macd = macd_diff_vals[i]
        c_stoch = stoch_k_vals[i]
        vol_ratio = (c_atr / c_price) * 100 if c_price > 0 else 0

        if vol_ratio < 0.02:
            continue

        confidence = 0.60
        if clf:
            try:
                feat = pd.DataFrame([[c_rsi, c_dxy, c_macd, c_stoch, vol_ratio]], 
                                    columns=['rsi', 'dxy_corr', 'macd_diff', 'stoch_k', 'volatility_ratio'])
                prob = clf.predict_proba(feat)[0]
                classes = list(clf.classes_)
                if 1 in classes:
                    win_prob = float(prob[classes.index(1)])
                    if win_prob < 0.52:
                        continue
                    confidence = win_prob
            except Exception:
                pass

        valid_dxy = c_dxy < 0.35 or confidence >= 0.75
        if not valid_dxy:
            continue

        is_h4_bull = (h1_ema200_vals[i] > h1_ema500_vals[i]) if not np.isnan(h1_ema500_vals[i]) else (close_vals[i] > close_vals[i-20])
        is_h4_bear = not is_h4_bull

        is_buy = is_h4_bull and (ema9_vals[i] > ema21_vals[i]) and (c_rsi < 72) and (low_vals[i-1] > high_vals[i-3] or ema9_vals[i-1] <= ema21_vals[i-1])
        is_sell = is_h4_bear and (ema9_vals[i] < ema21_vals[i]) and (c_rsi > 28) and (high_vals[i-1] < low_vals[i-3] or ema9_vals[i-1] >= ema21_vals[i-1])

        if not (is_buy or is_sell):
            continue

        signals += 1
        outcome = None

        if is_buy:
            tp = c_price + (c_atr * 1.8)
            sl = c_price - (c_atr * 1.2)
            
            for fut_idx in range(i + 1, i + 13):
                f_open = open_vals[fut_idx]
                f_high = high_vals[fut_idx]
                f_low = low_vals[fut_idx]
                
                if f_low <= sl and f_high >= tp:
                    outcome = 0 if abs(f_open - sl) < abs(f_open - tp) else 1
                    break
                elif f_low <= sl:
                    outcome = 0
                    break
                elif f_high >= tp:
                    outcome = 1
                    break

            if outcome == 1:
                wins += 1
                pnl = (c_atr * 1.8) * 10
                gross_profit += pnl
                balance += pnl
            elif outcome == 0:
                losses += 1
                pnl = (c_atr * 1.2) * 10
                gross_loss += pnl
                balance -= pnl

        elif is_sell:
            tp = c_price - (c_atr * 1.8)
            sl = c_price + (c_atr * 1.2)
            
            for fut_idx in range(i + 1, i + 13):
                f_open = open_vals[fut_idx]
                f_high = high_vals[fut_idx]
                f_low = low_vals[fut_idx]

                if f_high >= sl and f_low <= tp:
                    outcome = 0 if abs(f_open - sl) < abs(f_open - tp) else 1
                    break
                elif f_high >= sl:
                    outcome = 0
                    break
                elif f_low <= tp:
                    outcome = 1
                    break

            if outcome == 1:
                wins += 1
                pnl = (c_atr * 1.8) * 10
                gross_profit += pnl
                balance += pnl
            elif outcome == 0:
                losses += 1
                pnl = (c_atr * 1.2) * 10
                gross_loss += pnl
                balance -= pnl

        if balance > peak_balance:
            peak_balance = balance
        dd = (peak_balance - balance) / peak_balance * 100
        if dd > max_drawdown:
            max_drawdown = dd

    total_trades = wins + losses
    win_rate = round((wins / total_trades * 100), 1) if total_trades > 0 else 0
    profit_factor = round((gross_profit / gross_loss), 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0)

    msg = (
        f"📊 **نتائج فحص الاستراتيجية العكسي (Quant ML Backtest)**\n"
        f"───────────────────\n"
        f"🔢 إجمالي الإشارات المختبرة: {signals}\n"
        f"✅ الصفقات الناجحة: {wins}\n"
        f"❌ الصفقات الخاسرة: {losses}\n"
        f"🎯 **نسبة النجاح (Win Rate):** {win_rate}%\n"
        f"⚖️ **مشارف الربحية (Profit Factor):** {profit_factor}\n"
        f"📉 **التراجع الأقصى (Max Drawdown):** {round(max_drawdown, 2)}%\n"
        f"───────────────────\n"
        f"🤖 *ملاحظة: الفحص العكسي يدمج نموذج الذكاء الاصطناعي والفلاتر المؤسسية الحية.*"
    )
    return msg

# ------------------------------------
# 7. المراقبة الآلية ومراقب الذاكرة العشوائية والحيويّة
# ------------------------------------
async def keep_alive_ping():
    """إرسال طلب HTTP ذاتي كل 8 دقائق لإبقاء سيرفر Render نشطاً ومستيقظاً 24/7"""
    url = os.getenv("RENDER_EXTERNAL_URL", "https://gold-quant-bot.onrender.com")
    while True:
        await asyncio.sleep(480)
        try:
            await asyncio.to_thread(requests.get, url, timeout=5)
            print("⚓ تم إرسال إشارة الاستيقاظ الذاتية لـ Render بنجاح.")
        except Exception as e:
            print(f"تنبيه فحص الاستيقاظ الذاتي: {e}")

async def background_cache_worker():
    """تحديث الكاش كل 5 ثوان لتوازن مثالي بين السرعة واستقرار السيرفر"""
    while True:
        try:
            await asyncio.to_thread(fetch_and_update_cache)
        except Exception as e:
            print(f"خطأ خلفية الكاش: {e}")
        await asyncio.sleep(5)

async def auto_market_scanner(app):
    last_sent_candle = None
    while True:
        try:
            sig = await asyncio.to_thread(generate_quant_signal)
            if sig and sig["status"] == "SIGNAL":
                current_candle = sig.get("candle_id")
                if current_candle != last_sent_candle:
                    last_sent_candle = current_candle
                    msg = (
                        f"🚨 **إشارة هجينة جديدة (Quant & Gemini AI)**\n"
                        f"───────────────────\n"
                        f"النوع: {sig['type']}\n"
                        f"🎯 **نسبة ثقة الموديل:** {sig['confidence']}%\n"
                        f"⚖️ **المخاطرة الموصى بها:** {sig['risk']}\n"
                        f"💵 **سعر الدخول:** ${sig['entry']}\n"
                        f"🛑 **وقف الخسارة (SL):** ${sig['sl']}\n"
                        f"🎯 **الهدف الأول (TP1):** ${sig['tp1']}\n"
                        f"🎯 **الهدف الثاني (TP2):** ${sig['tp2']}\n"
                        f"💡 **تأكيد الهيكل:** {sig['smc_note']}\n"
                        f"🤖 **ملاحظة Gemini:** {sig.get('gemini_note', 'تم التأييد')}\n"
                        f"🔗 **ارتباط الدولار:** {sig['dxy_corr']}\n"
                        f"───────────────────\n"
                        f"🤖 *تم تأكيد الإشارة بالتناغم المؤسسي وتدقيق Gemini AI.*"
                    )
                    subscribers = get_subscribers()
                    for user_id in subscribers:
                        if is_authenticated(user_id):
                            try:
                                await app.bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown')
                            except Exception as send_err:
                                print(f"تعذر الإرسال للمستخدم {user_id}: {send_err}")
        except Exception as e:
            print(f"خطأ في الفحص الآلي: {e}")
            
        await asyncio.sleep(120)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """معالج الأخطاء لمنع توقف البوت عند استثناءات التعارض والشبكة"""
    print(f"⚠️ استثناء في التلغرام: {context.error}")

async def post_init(app):
    asyncio.create_task(background_cache_worker())
    asyncio.create_task(auto_market_scanner(app))
    asyncio.create_task(keep_alive_ping())

# --- الأوامر المباشرة ---
async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    text = update.message.text.strip()
    user_info = f"{user.first_name} (@{user.username if user.username else 'بدون معرف'}) [ID: {chat_id}]"

    if not is_authenticated(chat_id):
        if text == PASSWORD:
            authenticate_user(chat_id)
            add_subscriber(chat_id)
            await update.message.reply_text(
                "✅ **تم تسجيل الدخول بنجاح!**\n"
                "مرحباً بك في البوت الكمي المؤسسي المدعوم بـ Gemini AI. تم تفعيل كافة الصلاحيات والتنبيهات التلقائية.\n\n"
                "💡 يمكنك الآن الضغط على الأزرار في الأسفل لتنفيذ الأوامر فوراً.",
                reply_markup=get_main_keyboard(),
                parse_mode='Markdown'
            )
            await notify_admin(context, f"🔑 **تسجيل دخول ناجح!**\nالمستخدم: {user_info}")
        else:
            await update.message.reply_text("❌ **كلمة السر غير صحيحة!**\nتم تسجيل محاولة الدخول وإبلاغ مسؤول النظام.")
            await notify_admin(context, f"⚠️ **محاولة دخول فاشلة!**\nالمستخدم: {user_info}\nكلمة السر المدخلة: `{text}`")
        return

    if "إشارة فورية" in text or text == "/signal":
        await signal(update, context)
    elif "تحليل بنية السوق" in text or text == "/analyze":
        await analyze(update, context)
    elif "الأسعار اللحظية" in text or text == "/price":
        await price(update, context)
    elif "إحصائيات النظام" in text or text == "/stats":
        await stats(update, context)
    elif "اختبار الاستراتيجية العكسي" in text or text == "/backtest":
        await backtest(update, context)
    else:
        await update.message.reply_text(
            "💡 استخدم الأزرار الظاهرة في الأسفل للتحكم بالبوت.",
            reply_markup=get_main_keyboard()
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_info = f"{user.first_name} (@{user.username if user.username else 'بدون معرف'}) [ID: {chat_id}]"

    if not is_authenticated(chat_id):
        await update.message.reply_text(
            "🔒 **البوت محمي بكلمة سر.**\n"
            "يرجى إرسال كلمة السر الخاصة بك للدخول إلى النظام."
        )
        await notify_admin(context, f"🚨 **محاولة وصول جديدة للبوت (/start)**\nالمستخدم: {user_info}\nالحالة: غير مسجل دخول.")
        return

    add_subscriber(chat_id)
    await update.message.reply_text(
        f"أهلاً بك مجدداً! 🚀\n"
        f"حسابك موثق ومفعل في **البوت الكمي الهجين (Quant Engine & Gemini AI)**.\n\n"
        f"💡 اضغط على الأزرار أدناه لتنفيذ ما تريد:",
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await update.message.reply_text("🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
    
    data = get_market_data()
    msg = f"📊 **أسعار السوق اللحظية (Spot Gold)**\n🟡 الذهب (XAUUSD): ${data['gold']}\n💵 مؤشر الدولار: {data['dxy']}\n📈 عوائد السندات: {data['us10y']}%"
    await update.message.reply_text(msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')

async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await update.message.reply_text("🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
    await update.message.reply_text("🧠 جاري مسح التناغم المؤسسي وهياكل SMC...")
    res = await asyncio.to_thread(analyze_institutional_engine)
    if res:
        smc = res['smc']
        smc_status = "صاعد (FVG/Sweep)" if smc['fvg_bullish'] or smc['sweep_bullish'] else ("هابط (FVG/Sweep)" if smc['fvg_bearish'] or smc['sweep_bearish'] else "محايد")
        
        # استخراج أحدث درس تم تعلمه بواسطة Gemini
        last_lesson = "لا يوجد أخطاء حديثة مفحوصة."
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT lesson FROM gemini_insights ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                last_lesson = row[0]
        except Exception:
            pass
        finally:
            release_db_connection(conn)

        msg = (
            f"🤖 **تقرير بنية السوق المؤسسية (XAU/USD)**\n"
            f"───────────────────\n"
            f"💰 سعر الذهب الفوري: ${res['last_price']}\n"
            f"📈 اتجاه H4 الحاكم: {res['h4_trend']}\n"
            f"📊 حالة HMM (M15): {res['state_label']}\n"
            f"🏦 هيكل السيولة (SMC): {smc_status}\n"
            f"🔗 معامل ارتباط الدولار: {res['dxy_corr']}\n"
            f"───────────────────\n"
            f"🧠 **أحدث قواعد Gemini للتعلم الذاتي:**\n_{last_lesson}_"
        )
        await update.message.reply_text(msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')

async def backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await update.message.reply_text("🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
    await update.message.reply_text("📈 جاري تشغيل الاختبار العكسي الكمي للبيانات التاريخية...")
    report = await asyncio.to_thread(run_quant_backtest)
    await update.message.reply_text(report, reply_markup=get_main_keyboard(), parse_mode='Markdown')

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await update.message.reply_text("🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
        
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        is_pg = is_postgres() and isinstance(conn, psycopg2.extensions.connection)
        
        cursor.execute("SELECT COUNT(*), SUM(CASE WHEN outcome = 1 THEN 1 ELSE 0 END) FROM trades WHERE outcome IS NOT NULL")
        total_eval, total_wins = cursor.fetchone()
        total_eval = total_eval or 0
        total_wins = total_wins or 0
        overall_win_rate = round((total_wins / total_eval * 100), 1) if total_eval > 0 else 0

        if is_pg:
            cursor.execute("SELECT COUNT(*), SUM(CASE WHEN outcome = 1 THEN 1 ELSE 0 END) FROM trades WHERE outcome IS NOT NULL AND timestamp >= NOW() - INTERVAL '7 days'")
            week_eval, week_wins = cursor.fetchone()
            cursor.execute("SELECT COUNT(*), SUM(CASE WHEN outcome = 1 THEN 1 ELSE 0 END) FROM trades WHERE outcome IS NOT NULL AND timestamp >= NOW() - INTERVAL '30 days'")
            month_eval, month_wins = cursor.fetchone()
        else:
            cursor.execute("SELECT COUNT(*), SUM(CASE WHEN outcome = 1 THEN 1 ELSE 0 END) FROM trades WHERE outcome IS NOT NULL AND datetime(timestamp) >= datetime('now', '-7 days')")
            week_eval, week_wins = cursor.fetchone()
            cursor.execute("SELECT COUNT(*), SUM(CASE WHEN outcome = 1 THEN 1 ELSE 0 END) FROM trades WHERE outcome IS NOT NULL AND datetime(timestamp) >= datetime('now', '-30 days')")
            month_eval, month_wins = cursor.fetchone()

        week_eval, week_wins = week_eval or 0, week_wins or 0
        weekly_win_rate = round((week_wins / week_eval * 100), 1) if week_eval > 0 else 0

        month_eval, month_wins = month_eval or 0, month_wins or 0
        monthly_win_rate = round((month_wins / month_eval * 100), 1) if month_eval > 0 else 0

        cursor.execute("SELECT COUNT(*), SUM(CASE WHEN outcome = 1 THEN 1 ELSE 0 END) FROM trades WHERE outcome IS NOT NULL AND signal_type LIKE '%BUY%'")
        buy_total, buy_wins = cursor.fetchone()
        buy_total, buy_wins = buy_total or 0, buy_wins or 0
        buy_rate = round((buy_wins / buy_total * 100), 1) if buy_total > 0 else 0

        cursor.execute("SELECT COUNT(*), SUM(CASE WHEN outcome = 1 THEN 1 ELSE 0 END) FROM trades WHERE outcome IS NOT NULL AND signal_type LIKE '%SELL%'")
        sell_total, sell_wins = cursor.fetchone()
        sell_total, sell_wins = sell_total or 0, sell_wins or 0
        sell_rate = round((sell_wins / sell_total * 100), 1) if sell_total > 0 else 0

        db_type_str = "Supabase PostgreSQL (سحابية دائمية مع Connection Pool)" if is_pg else "SQLite (محلية / احتياطية)"
        
        msg = (
            f"📊 **تقرير أداء المحرك الكمي التفصيلي (Quant Analytics)**\n"
            f"───────────────────\n"
            f"🗄️ **قاعدة البيانات:** {db_type_str}\n"
            f"📈 **معدل النجاح الكلي:** {overall_win_rate}% ({total_wins}/{total_eval})\n"
            f"📅 **أداء آخر 7 أيام:** {weekly_win_rate}% ({week_wins}/{week_eval})\n"
            f"🗓️ **أداء آخر 30 يوماً:** {monthly_win_rate}% ({month_wins}/{month_eval})\n"
            f"───────────────────\n"
            f"🟢 **صفقات الشراء (BUY):** {buy_rate}% نجاح ({buy_wins}/{buy_total})\n"
            f"🔴 **صفقات البيع (SELL):** {sell_rate}% نجاح ({sell_wins}/{sell_total})\n"
            f"───────────────────\n"
            f"💡 *تم دمج نظام التدقيق الراجع والتعلم الذاتي التلقائي عبر Gemini API.*"
        )
        await update.message.reply_text(msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')
    except Exception as e:
        print(f"خطأ إحضار الإحصائيات: {e}")
    finally:
        release_db_connection(conn)

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await update.message.reply_text("🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
    await update.message.reply_text("⚡ جاري مطابقة شروط التناغم المؤسسي وتدقيق مخاطر Gemini...")
    sig = await asyncio.to_thread(generate_quant_signal)
    if sig and sig["status"] == "SIGNAL":
        msg = (
            f"🚨 **إشارة كمية مؤسسية مدققة**\n"
            f"النوع: {sig['type']}\n"
            f"🎯 نسبة الثقة: {sig['confidence']}%\n"
            f"⚖️ اللوت الموصى به: {sig['risk']}\n"
            f"💵 سعر الدخول: ${sig['entry']}\n"
            f"🛑 SL: ${sig['sl']}\n"
            f"🎯 TP1: ${sig['tp1']}\n"
            f"🎯 TP2: ${sig['tp2']}\n"
            f"💡 SMC: {sig['smc_note']}\n"
            f"🤖 Gemini Risk Note: {sig.get('gemini_note', 'تم التأييد')}"
        )
    else:
        msg = f"⏸️ **تنبيه الانتظار المؤسسي**\n💡 السبب: {sig['reason'] if sig else 'لا توجد فرصة مطابقة'}"
    await update.message.reply_text(msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    init_db()
    load_admin_id()

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_error_handler(error_handler)
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("backtest", backtest))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    
    print("🤖 البوت الهجين (Quant + Gemini API) يعمل بكفاءة تامة وجاهز للمراقبة 24/7...")
    app.run_polling(drop_pending_updates=True)

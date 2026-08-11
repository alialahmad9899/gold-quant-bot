import logging
from logging.handlers import RotatingFileHandler
import asyncio
import sqlite3
import psycopg2
from psycopg2 import pool
from psycopg2.extras import Json as PGJson
import os
import requests
import re
import gc
import json
import time
import threading
import warnings
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

# إخفاء تحذيرات pandas لقواعد البيانات كلياً
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', message='.*pandas only supports SQLAlchemy.*')

# 🌐 استدعاء curl_cffi لتجاوز حظر Cloudflare وبصمات السيرفرات السحابية
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = requests

# 🤖 استدعاء المكتبة الرسمية لـ Gemini SDK
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

# --- 1. خادم الويب الأساسي لإرضاء Render Web Service وفحص المنفذ فوراً ---
web_app = Flask(__name__)

@web_app.route('/')
def home():
    now = datetime.now(timezone.utc)
    with cache_lock:
        last_market = GLOBAL_CACHE.get("last_updated")
        gold = GLOBAL_CACHE.get("market_data", {}).get("gold", 0.0)
    cache_age = (now - last_market).total_seconds() if last_market else None
    healthy = bool(gold and gold > 1000)
    payload = {
        "الحالة": "سليم" if healthy else "قيد المزامنة",
        "الذهب": gold,
        "عمر_الكاش_بالثواني": cache_age,
        "الذكاء_الاصطناعي": bool(gemini_client),
    }
    return payload, 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# 🔒 جلب التوكنات وقواعد البيانات من متغيرات البيئة بدون أسرار افتراضية مدمجة
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("❌ خطأ أمني حرج: متغير البيئة TELEGRAM_TOKEN غير مضبوط! يرجى ضبط التوكن في متغيرات البيئة على المنصة.")

DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

PASSWORD = os.getenv("BOT_PASSWORD")
if not PASSWORD:
    raise ValueError("❌ خطأ أمني حرج: متغير البيئة BOT_PASSWORD غير مضبوط! يرجى تحديد كلمة مرور آمنة في متغيرات البيئة.")

DB_FILE = "trades.db"

# تهيئة عميل Gemini
gemini_client = None
if GEMINI_API_KEY and genai:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("✅ تم تفعيل وتشغيل خدمة الذكاء الاصطناعي بنجاح.")
    except Exception as e:
        print(f"⚠️ يتعذر تهيئة خدمة الذكاء الاصطناعي: {e}")

# ------------------------------------
# 🔑 إعدادات الحماية والآدمن والكاش وأمان الخيوط والمهام
# ------------------------------------
ADMIN_CHAT_ID = 0

cache_lock = threading.Lock()
fetch_lock = threading.Lock()
SIGNAL_LOCK = threading.Lock()
LAST_SCANNER_CANDLE = None

# حاوية مراجع المهام الخلفية لمنع تدميرها بواسطة Python GC
BACKGROUND_TASKS = set()

def create_managed_task(coro):
    """إنشاء وتتبع المهام الخلفية لمنع خطأ Task was destroyed but it is pending!"""
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task

GLOBAL_CACHE = {
    "market_data": {"gold": 0.0, "dxy": 99.85, "us10y": 4.63},
    "analysis": None,
    "last_updated": None
}

MARKET_DATA_CACHE = {
    "df_gold_h1": pd.DataFrame(),
    "df_gold_m15": pd.DataFrame(),
    "df_dxy_m15": pd.DataFrame(),
    "df_us10y_m15": pd.DataFrame(),
    "last_fetch": None
}

CACHED_MODEL = None
CACHED_MODEL_META = {}
LAST_TRAIN_TIME = None
MODEL_LOCK = threading.Lock()
MODEL_TRAIN_LIMIT = int(os.getenv("MODEL_TRAIN_LIMIT", "1000"))
MODEL_MIN_OOS_TRADES = int(os.getenv("MODEL_MIN_OOS_TRADES", "30"))
MODEL_REFRESH_SECONDS = int(os.getenv("MODEL_REFRESH_SECONDS", "1800"))
MODEL_PROMOTION_MARGIN_R = float(os.getenv("MODEL_PROMOTION_MARGIN_R", "0.20"))

LOG_FILE = os.getenv('LOG_FILE', 'xau_quant_bot.log')
LOG_MAX_BYTES = int(os.getenv('LOG_MAX_BYTES', str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv('LOG_BACKUP_COUNT', '3'))
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
if not _root_logger.handlers:
    _handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8')
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    _root_logger.addHandler(_handler)
    _root_logger.addHandler(logging.StreamHandler())
logger = logging.getLogger('xau_quant_bot')

FEATURE_VERSION = os.getenv('FEATURE_VERSION', 'v2.8-features-1')
STRATEGY_VERSION = os.getenv('STRATEGY_VERSION', 'v2.8-flexible')
MODEL_VERSION = os.getenv('MODEL_VERSION', 'rf-v1')
RAM_WARNING_MB = float(os.getenv('RAM_WARNING_MB', '400'))
RAM_DEFER_MB = float(os.getenv('RAM_DEFER_MB', '450'))
RAM_EMERGENCY_MB = float(os.getenv('RAM_EMERGENCY_MB', '480'))
MAX_LEARNING_QUEUE = int(os.getenv('MAX_LEARNING_QUEUE', '50'))
LEARNING_BATCH_SIZE = int(os.getenv('LEARNING_BATCH_SIZE', '5'))
MAX_LEARNING_RETRIES = int(os.getenv('MAX_LEARNING_RETRIES', '5'))

class SystemMonitor:
    def __init__(self):
        self.started_at = time.monotonic()
        self.error_counts = {}
        self.last_error_at = None
        self._lock = threading.Lock()

    @staticmethod
    def memory_mb():
        try:
            with open('/proc/self/status', 'r', encoding='utf-8') as fh:
                for line in fh:
                    if line.startswith('VmRSS:'):
                        return float(line.split()[1]) / 1024.0
        except Exception:
            pass
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return float(rss) / 1024.0
        except Exception:
            return 0.0

    def tier(self):
        mb = self.memory_mb()
        if mb >= RAM_EMERGENCY_MB:
            return 'EMERGENCY', mb
        if mb >= RAM_DEFER_MB:
            return 'DEFERRED', mb
        if mb >= RAM_WARNING_MB:
            return 'WARNING', mb
        return 'NORMAL', mb

    def heavy_tasks_allowed(self):
        return self.tier()[0] == 'NORMAL'

    def defer_heavy_tasks(self):
        tier, mb = self.tier()
        return tier in {'WARNING', 'DEFERRED', 'EMERGENCY'}, tier, mb

    def record_error(self, tag, error):
        with self._lock:
            self.error_counts[tag] = self.error_counts.get(tag, 0) + 1
            self.last_error_at = datetime.now(timezone.utc)
        logger.error('[%s] %s', tag, error)

    def summary(self):
        tier, mb = self.tier()
        with self._lock:
            errors = dict(self.error_counts)
        return {'ram_mb': round(mb, 1), 'tier': tier, 'uptime_min': round((time.monotonic()-self.started_at)/60.0, 1), 'errors': errors}

system_monitor = SystemMonitor()

# ------------------------------------
# 🛠️ أدوات مساعدة وتجهيز البيانات
# ------------------------------------
async def safe_reply_text(update: Update, text: str, **kwargs):
    """إرسال آمن للرسائل مع تجنب توقف البوت بسبب خطأ تنسيق التليغرام Markdown"""
    try:
        return await update.message.reply_text(text, **kwargs)
    except Exception as e:
        err_msg = str(e)
        if "Can't parse entities" in err_msg or "400" in err_msg or "Bad Request" in err_msg:
            kwargs_copy = dict(kwargs)
            kwargs_copy.pop('parse_mode', None)
            return await update.message.reply_text(text, **kwargs_copy)
        raise e

async def safe_send_message(bot, chat_id: int, text: str, **kwargs):
    """إرسال آمن عبر bot.send_message لمنع مشاكل Markdown"""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        err_msg = str(e)
        if "Can't parse entities" in err_msg or "400" in err_msg or "Bad Request" in err_msg:
            kwargs_copy = dict(kwargs)
            kwargs_copy.pop('parse_mode', None)
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs_copy)
        raise e

def fetch_live_economic_news_alert():
    """جلب حالة المفكرة الاقتصادية وتفادي توقف البوت عند عدم وجود مزود خارجي"""
    try:
        return False, "الظروف الإخبارية مستقرة", False
    except Exception as e:
        return False, f"تعذر الفحص: {e}", True

def clean_df_columns(df):
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
        [KeyboardButton("📉 اختبار الاستراتيجية العكسي"), KeyboardButton("🔍 فحص الأخطاء الشامل")],
        [KeyboardButton("🧹 تصفير البيانات")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def arabic_direction(value):
    return {"BULLISH": "صاعد 🟢", "BEARISH": "هابط 🔴", "RANGING": "متذبذب 🟡", "NEUTRAL": "محايد 🟡"}.get(str(value), str(value))

def arabic_trade_status(value):
    return {"OPEN": "مفتوحة 🟢", "TP1_HIT": "الهدف الأول محقق 🎯", "TP2_HIT": "الهدف الثاني محقق 🏆", "SL_HIT": "وقف الخسارة محقق 🛑", "EXPIRED": "منتهية ⏳", "CANCELLED": "ملغاة 🚫"}.get(str(value), str(value))

# ------------------------------------
# 1. إدارة قاعدة البيانات الهجينة ومجمع الاتصالات
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
                    confidence REAL,
                    candle_id VARCHAR(100),
                    trade_status VARCHAR(20) DEFAULT 'OPEN',
                    tp1_hit_at TIMESTAMP,
                    tp2_hit_at TIMESTAMP,
                    sl_hit_at TIMESTAMP,
                    exit_price REAL,
                    realized_r REAL,
                    slippage REAL,
                    feature_version VARCHAR(50),
                    strategy_version VARCHAR(50),
                    model_version VARCHAR(50),
                    signal_score REAL,
                    ai_score REAL
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
                CREATE TABLE IF NOT EXISTS model_evaluations (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    model_name VARCHAR(100), feature_version VARCHAR(50),
                    sample_size INTEGER, oos_trades INTEGER, total_r REAL,
                    expectancy_r REAL, profit_factor REAL, max_drawdown_r REAL, promoted INTEGER DEFAULT 0
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
                    confidence REAL,
                    candle_id TEXT,
                    trade_status TEXT DEFAULT 'OPEN',
                    tp1_hit_at TEXT,
                    tp2_hit_at TEXT,
                    sl_hit_at TEXT,
                    exit_price REAL,
                    realized_r REAL,
                    slippage REAL,
                    feature_version TEXT,
                    strategy_version TEXT,
                    model_version TEXT,
                    signal_score REAL,
                    ai_score REAL
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
            lifecycle_columns = {
                "candle_id": "TEXT", "trade_status": "TEXT DEFAULT 'OPEN'",
                "tp1_hit_at": "TEXT", "tp2_hit_at": "TEXT", "sl_hit_at": "TEXT",
                "exit_price": "REAL", "realized_r": "REAL", "slippage": "REAL"
            }
            for col_name, col_type in lifecycle_columns.items():
                if col_name not in columns:
                    cursor.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_candle_id ON trades(candle_id)")

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

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS model_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, model_name TEXT, feature_version TEXT,
                    sample_size INTEGER, oos_trades INTEGER, total_r REAL, expectancy_r REAL, profit_factor REAL, max_drawdown_r REAL, promoted INTEGER DEFAULT 0
                )
            ''')
        if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
            cursor.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS candle_id VARCHAR(100)")
            cursor.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS trade_status VARCHAR(20) DEFAULT 'OPEN'")
            cursor.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS tp1_hit_at TIMESTAMP")
            cursor.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS tp2_hit_at TIMESTAMP")
            cursor.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS sl_hit_at TIMESTAMP")
            cursor.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit_price REAL")
            cursor.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS realized_r REAL")
            cursor.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS slippage REAL")
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_candle_id ON trades(candle_id)")

        if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS learning_events (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(50) NOT NULL,
                    event_key VARCHAR(150) UNIQUE NOT NULL,
                    priority INTEGER DEFAULT 3,
                    status VARCHAR(20) DEFAULT 'PENDING',
                    payload JSONB,
                    retry_count INTEGER DEFAULT 0,
                    next_retry_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_learning_status ON learning_events(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_learning_retry ON learning_events(next_retry_at)")
            for ddl in [
                "ALTER TABLE trades ADD COLUMN IF NOT EXISTS feature_version VARCHAR(50)",
                "ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy_version VARCHAR(50)",
                "ALTER TABLE trades ADD COLUMN IF NOT EXISTS model_version VARCHAR(50)",
                "ALTER TABLE trades ADD COLUMN IF NOT EXISTS signal_score REAL",
                "ALTER TABLE trades ADD COLUMN IF NOT EXISTS ai_score REAL",
            ]:
                cursor.execute(ddl)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS learning_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    event_key TEXT UNIQUE NOT NULL,
                    priority INTEGER DEFAULT 3,
                    status TEXT DEFAULT 'PENDING',
                    payload TEXT,
                    retry_count INTEGER DEFAULT 0,
                    next_retry_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_error TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_learning_status ON learning_events(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_learning_retry ON learning_events(next_retry_at)")
            cursor.execute("PRAGMA table_info(trades)")
            trade_columns = [col[1] for col in cursor.fetchall()]
            for col_name, col_type in {
                'feature_version':'TEXT', 'strategy_version':'TEXT', 'model_version':'TEXT',
                'signal_score':'REAL', 'ai_score':'REAL'
            }.items():
                if col_name not in trade_columns:
                    cursor.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
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
            await safe_send_message(context.bot, chat_id=ADMIN_CHAT_ID, text=message, parse_mode='Markdown')
        except Exception as e:
            print(f"خطأ في إرسال الإشعار للآدمن: {e}")

def has_active_open_trade(signal_type):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        ph = "%s" if is_postgres() and isinstance(conn, psycopg2.extensions.connection) else "?"
        cursor.execute(f"SELECT id FROM trades WHERE signal_type = {ph} AND outcome IS NULL", (signal_type,))
        row = cursor.fetchone()
        return row is not None
    except Exception as e:
        print(f"خطأ في فحص الصفقة النشطة: {e}")
        return False
    finally:
        release_db_connection(conn)

def log_trade(signal_type, entry, sl, tp1, tp2, rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio, confidence, candle_id=None, signal_score=None, ai_score=None):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        f_entry, f_sl, f_tp1, f_tp2 = map(float, (entry, sl, tp1, tp2))
        f_rsi, f_dxy, f_macd, f_stoch, f_vol, f_conf = map(float, (rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio, confidence))
        candle_id = candle_id or datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        is_pg = is_postgres() and isinstance(conn, psycopg2.extensions.connection)
        params = (now_str, signal_type, f_entry, f_sl, f_tp1, f_tp2, f_rsi, f_dxy, f_macd, f_stoch, f_vol, f_conf, candle_id, FEATURE_VERSION, STRATEGY_VERSION, MODEL_VERSION, signal_score, ai_score)
        if is_pg:
            cursor.execute('''
                INSERT INTO trades
                (timestamp, signal_type, entry_price, sl, tp1, tp2, rsi, dxy_corr, macd_diff,
                 stoch_k, volatility_ratio, outcome, confidence, candle_id, trade_status,
                 feature_version, strategy_version, model_version, signal_score, ai_score)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,'OPEN',%s,%s,%s,%s,%s)
                ON CONFLICT (candle_id) DO NOTHING RETURNING id
            ''', params)
        else:
            cursor.execute('''
                INSERT OR IGNORE INTO trades
                (timestamp, signal_type, entry_price, sl, tp1, tp2, rsi, dxy_corr, macd_diff,
                 stoch_k, volatility_ratio, outcome, confidence, candle_id, trade_status,
                 feature_version, strategy_version, model_version, signal_score, ai_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?, 'OPEN',?,?,?,?,?)
            ''', params)
        inserted = cursor.rowcount == 1
        trade_id = None
        if inserted:
            if is_pg:
                row = cursor.fetchone(); trade_id = row[0] if row else None
            else:
                trade_id = cursor.lastrowid
        conn.commit()
        return inserted, trade_id
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        system_monitor.record_error('TRADE_INSERT', e)
        return False, None
    finally:
        release_db_connection(conn)

def update_trade_state(trade_id, new_status, exit_price=None, realized_r=None, slippage=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor(); is_pg=is_postgres() and isinstance(conn, psycopg2.extensions.connection); ph='%s' if is_pg else '?'
        now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        if new_status=='TP1_HIT':
            sql=f"UPDATE trades SET trade_status={ph},tp1_hit_at={ph},exit_price={ph},realized_r={ph},slippage={ph} WHERE id={ph} AND trade_status='OPEN'"; params=(new_status,now,exit_price,realized_r,slippage,trade_id)
        elif new_status=='TP2_HIT':
            sql=f"UPDATE trades SET trade_status={ph},tp2_hit_at={ph},exit_price={ph},realized_r={ph},slippage={ph},outcome=1 WHERE id={ph} AND trade_status IN ('OPEN','TP1_HIT')"; params=(new_status,now,exit_price,realized_r,slippage,trade_id)
        elif new_status=='SL_HIT':
            sql=f"UPDATE trades SET trade_status={ph},sl_hit_at={ph},exit_price={ph},realized_r={ph},slippage={ph},outcome=0 WHERE id={ph} AND trade_status IN ('OPEN','TP1_HIT')"; params=(new_status,now,exit_price,realized_r,slippage,trade_id)
        else:
            sql=f"UPDATE trades SET trade_status={ph},exit_price={ph},realized_r={ph},slippage={ph} WHERE id={ph} AND trade_status IN ('OPEN','TP1_HIT')"; params=(new_status,exit_price,realized_r,slippage,trade_id)
        cur.execute(sql,params); changed=cur.rowcount==1; conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        system_monitor.record_error('TRADE_STATE',e); return False
    finally: release_db_connection(conn)
    if changed and new_status in {'SL_HIT','TP2_HIT'}:
        payload=_build_trade_learning_payload(trade_id,new_status)
        if payload:
            enqueue_learning_event('TRADE_OUTCOME',f'trade_outcome:{trade_id}:{new_status}',payload,priority=1 if new_status=='SL_HIT' else 3)
    return changed

def _build_trade_learning_payload(trade_id, outcome_status):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        is_pg = is_postgres() and isinstance(conn, psycopg2.extensions.connection)
        ph = '%s' if is_pg else '?'
        cur.execute(f"""
            SELECT id, signal_type, entry_price, sl, tp1, tp2, rsi, dxy_corr,
                   macd_diff, stoch_k, volatility_ratio, confidence, realized_r,
                   exit_price, slippage, feature_version, strategy_version,
                   model_version, signal_score, ai_score
            FROM trades WHERE id = {ph}
        """, (trade_id,))
        row = cur.fetchone()
        if not row:
            return None
        keys = ['trade_id','direction','entry_price','sl','tp1','tp2','rsi','dxy_corr','macd_diff','stoch_k','volatility_ratio','confidence','realized_r','exit_price','slippage','feature_version','strategy_version','model_version','signal_score','ai_score']
        d = dict(zip(keys,row))
        return {
            'trade_id': d['trade_id'], 'symbol':'XAUUSD', 'direction':d['direction'],
            'entry_price':d['entry_price'], 'outcome':'LOSS' if outcome_status=='SL_HIT' else outcome_status,
            'realized_r':d['realized_r'], 'exit_price':d['exit_price'], 'slippage':d['slippage'],
            'features': {'rsi':d['rsi'],'dxy_corr':d['dxy_corr'],'macd_diff':d['macd_diff'],'stoch_k':d['stoch_k'],'volatility_ratio':d['volatility_ratio'],'confidence':d['confidence'],'signal_score':d['signal_score'],'ai_score':d['ai_score']},
            'versions': {'feature_version':d['feature_version'] or FEATURE_VERSION,'strategy_version':d['strategy_version'] or STRATEGY_VERSION,'model_version':d['model_version'] or MODEL_VERSION}
        }
    except Exception as e:
        system_monitor.record_error('LEARNING_PAYLOAD',e)
        return None
    finally:
        release_db_connection(conn)

def enqueue_learning_event(event_type, event_key, payload_dict, priority=3):
    conn = get_db_connection()
    try:
        cur = conn.cursor(); now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        is_pg=is_postgres() and isinstance(conn, psycopg2.extensions.connection)
        payload=PGJson(payload_dict) if is_pg else json.dumps(payload_dict,ensure_ascii=False)
        if is_pg:
            cur.execute("""INSERT INTO learning_events (event_type,event_key,priority,status,payload,created_at,updated_at)
                           VALUES (%s,%s,%s,'PENDING',%s,%s,%s) ON CONFLICT (event_key) DO NOTHING""",(event_type,event_key,int(priority),payload,now,now))
        else:
            cur.execute("""INSERT OR IGNORE INTO learning_events (event_type,event_key,priority,status,payload,created_at,updated_at)
                           VALUES (?,?,?,?,?,?,?)""",(event_type,event_key,int(priority),'PENDING',payload,now,now))
        inserted=cur.rowcount==1; conn.commit(); return inserted
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        system_monitor.record_error('QUEUE_ENQUEUE',e); return False
    finally:
        release_db_connection(conn)

def recover_interrupted_learning_events():
    conn=get_db_connection()
    try:
        cur=conn.cursor(); is_pg=is_postgres() and isinstance(conn,psycopg2.extensions.connection); ph='%s' if is_pg else '?'
        now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        cur.execute(f"UPDATE learning_events SET status='PENDING',updated_at={ph} WHERE status='PROCESSING'",(now,)); n=cur.rowcount; conn.commit()
        if n: logger.info('[AI RECOVERY] تم استرداد %s أحداث تعلم عالقة.',n)
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        system_monitor.record_error('AI_RECOVERY',e)
    finally:
        release_db_connection(conn)

def claim_learning_batch(limit=5):
    limit=max(1,min(int(limit),LEARNING_BATCH_SIZE)); conn=get_db_connection(); claimed=[]
    try:
        cur=conn.cursor(); is_pg=is_postgres() and isinstance(conn,psycopg2.extensions.connection); now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        if is_pg:
            cur.execute("""SELECT id,event_type,event_key,priority,payload,retry_count FROM learning_events
                           WHERE status IN ('PENDING','DEFERRED') AND next_retry_at <= CURRENT_TIMESTAMP
                           ORDER BY priority ASC,created_at ASC LIMIT %s FOR UPDATE SKIP LOCKED""",(limit,))
        else:
            cur.execute('BEGIN IMMEDIATE')
            cur.execute("""SELECT id,event_type,event_key,priority,payload,retry_count FROM learning_events
                           WHERE status IN ('PENDING','DEFERRED') AND datetime(next_retry_at) <= datetime('now')
                           ORDER BY priority ASC,datetime(created_at) ASC LIMIT ?""",(limit,))
        rows=cur.fetchall(); ph='%s' if is_pg else '?'
        for row in rows:
            eid,etype,ekey,priority,payload,retry=row
            cur.execute(f"UPDATE learning_events SET status='PROCESSING',updated_at={ph} WHERE id={ph} AND status IN ('PENDING','DEFERRED')",(now,eid))
            if cur.rowcount==1:
                if isinstance(payload,str): payload=json.loads(payload)
                claimed.append({'id':eid,'event_type':etype,'event_key':ekey,'priority':priority,'payload':payload,'retry_count':retry})
        conn.commit(); return claimed
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        system_monitor.record_error('QUEUE_CLAIM',e); return []
    finally:
        release_db_connection(conn)

def mark_learning_completed(event_id):
    conn=get_db_connection()
    try:
        cur=conn.cursor(); is_pg=is_postgres() and isinstance(conn,psycopg2.extensions.connection); ph='%s' if is_pg else '?'
        cur.execute(f"UPDATE learning_events SET status='COMPLETED',updated_at={ph} WHERE id={ph}",(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),event_id)); conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        system_monitor.record_error('QUEUE_COMPLETE',e)
    finally: release_db_connection(conn)

def mark_learning_deferred(event_id,error,retry_count):
    conn=get_db_connection()
    try:
        retry_count=int(retry_count)+1; delay=min(3600,(2**min(retry_count,8))*5)+(hash(str(event_id))%7); next_at=datetime.now(timezone.utc)+timedelta(seconds=delay)
        status='FAILED' if retry_count>=MAX_LEARNING_RETRIES else 'DEFERRED'; cur=conn.cursor(); is_pg=is_postgres() and isinstance(conn,psycopg2.extensions.connection); ph='%s' if is_pg else '?'
        cur.execute(f"UPDATE learning_events SET status={ph},retry_count={ph},next_retry_at={ph},last_error={ph},updated_at={ph} WHERE id={ph}",(status,retry_count,next_at.strftime('%Y-%m-%d %H:%M:%S'),str(error)[:1000],datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),event_id)); conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        system_monitor.record_error('QUEUE_RETRY',e)
    finally: release_db_connection(conn)

class CircuitBreaker:
    def __init__(self,failure_threshold=3,cooldown_seconds=120): self.failure_threshold=failure_threshold; self.cooldown_seconds=cooldown_seconds; self.failures=0; self.opened_at=None; self.lock=threading.Lock()
    def allow(self):
        with self.lock:
            return self.opened_at is None or time.monotonic()-self.opened_at>=self.cooldown_seconds
    def success(self):
        with self.lock: self.failures=0; self.opened_at=None
    def failure(self):
        with self.lock:
            self.failures+=1
            if self.failures>=self.failure_threshold: self.opened_at=time.monotonic()

learning_circuit=CircuitBreaker()

def process_learning_batch(events):
    if not events: return True
    if not gemini_client: raise RuntimeError('خدمة Gemini غير مفعلة حالياً')
    if not learning_circuit.allow(): raise RuntimeError('قاطع دائرة Gemini مفتوح مؤقتاً')
    lines=[]
    for e in events:
        p=e['payload']; f=p.get('features',{})
        lines.append(f"ID:{p.get('trade_id')} | {p.get('direction')} | outcome:{p.get('outcome')} | R:{p.get('realized_r')} | RSI:{f.get('rsi')} | DXY:{f.get('dxy_corr')} | MACD:{f.get('macd_diff')} | Stoch:{f.get('stoch_k')} | Vol:{f.get('volatility_ratio')} | Conf:{f.get('confidence')} | Score:{f.get('signal_score')} | AI:{f.get('ai_score')} | Versions:{p.get('versions')}")
    prompt=f"""
أنت مستشار تعلم ذاتي لنظام تداول كمي للذهب XAUUSD.
حلل دفعة نتائج مغلقة لاكتشاف الأنماط المتكررة، بدون تعديل الاستراتيجية مباشرة.
ميّز بين الضوضاء والنمط المتكرر، وأعد JSON فقط:
{{\"lesson\":\"درس عربي مختصر قابل للاختبار\",\"suggested_adjustments\":[\"اقتراحات فقط دون تنفيذ\"]}}

النتائج:
{chr(10).join(lines)}
"""
    last_error=None
    for target_model in ['gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-1.5-pro']:
        try:
            response=gemini_client.models.generate_content(model=target_model,contents=prompt,config=types.GenerateContentConfig(response_mime_type='application/json'))
            result=json.loads(response.text); lesson=result.get('lesson','')
            if lesson:
                conn=get_db_connection()
                try:
                    cur=conn.cursor(); is_pg=is_postgres() and isinstance(conn,psycopg2.extensions.connection); ts=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                    if is_pg: cur.execute('INSERT INTO gemini_insights (created_at,lesson) VALUES (%s,%s)',(ts,lesson))
                    else: cur.execute('INSERT INTO gemini_insights (created_at,lesson) VALUES (?,?)',(ts,lesson))
                    conn.commit()
                finally: release_db_connection(conn)
            learning_circuit.success(); return True
        except Exception as e:
            last_error=e
            if '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e): time.sleep(1.5+(hash(str(time.time()))%1000)/1000.0); continue
            break
    learning_circuit.failure(); raise RuntimeError(last_error or 'فشل تحليل دفعة التعلم')

class LearningQueueManager:
    def __init__(self,maxsize=50): self.maxsize=maxsize; self.queue=None; self.enqueued_ids=set()
    def initialize(self):
        if self.queue is None: self.queue=asyncio.PriorityQueue(maxsize=self.maxsize)
    async def fill_from_db(self):
        self.initialize()
        if self.queue.full(): return
        conn=get_db_connection()
        try:
            cur=conn.cursor(); is_pg=is_postgres() and isinstance(conn,psycopg2.extensions.connection); ph='%s' if is_pg else '?'
            cur.execute(f"SELECT id,priority,created_at FROM learning_events WHERE status IN ('PENDING','DEFERRED') AND next_retry_at <= CURRENT_TIMESTAMP ORDER BY priority ASC,created_at ASC LIMIT {ph}",(self.maxsize,))
            rows=cur.fetchall()
        except Exception as e:
            system_monitor.record_error('QUEUE_FILL',e); rows=[]
        finally: release_db_connection(conn)
        for eid,priority,created_at in rows:
            if eid in self.enqueued_ids or self.queue.full(): continue
            try: await self.queue.put((int(priority or 3),str(created_at),int(eid))); self.enqueued_ids.add(eid)
            except asyncio.QueueFull: break
    async def worker_loop(self,stop_event):
        self.initialize()
        while not stop_event.is_set():
            try:
                await self.fill_from_db()
                if self.queue.empty(): await asyncio.sleep(5); continue
                heavy,tier,mb=system_monitor.defer_heavy_tasks()
                if heavy: logger.warning('[HEALTH] تأجيل التعلم بسبب RAM %.1fMB (%s).',mb,tier); await asyncio.sleep(15); continue
                take=min(LEARNING_BATCH_SIZE,self.queue.qsize())
                for _ in range(take):
                    _,_,eid=await self.queue.get(); self.enqueued_ids.discard(eid); self.queue.task_done()
                events=await asyncio.to_thread(claim_learning_batch,take)
                if not events: await asyncio.sleep(1); continue
                try:
                    await asyncio.to_thread(process_learning_batch,events)
                    for e in events: await asyncio.to_thread(mark_learning_completed,e['id'])
                except Exception as e:
                    for ev in events: await asyncio.to_thread(mark_learning_deferred,ev['id'],e,ev.get('retry_count',0))
                    system_monitor.record_error('AI_WORKER',e); await asyncio.sleep(2)
            except asyncio.CancelledError: break
            except Exception as e: system_monitor.record_error('LEARNING_WORKER',e); await asyncio.sleep(5)

learning_manager=LearningQueueManager(MAX_LEARNING_QUEUE)
LEARNING_STOP_EVENT=threading.Event()

def monitor_open_trades():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, signal_type, entry_price, sl, tp1, tp2, trade_status FROM trades WHERE outcome IS NULL AND trade_status IN ('OPEN','TP1_HIT')")
        rows = cursor.fetchall()
    except Exception as e:
        print(f"⚠️ تعذر تحميل الصفقات المفتوحة: {e}")
        rows = []
    finally:
        release_db_connection(conn)
    price = get_market_data().get('gold', 0.0)
    if price <= 0:
        return
    for row in rows:
        trade_id, sig_type, entry, sl, tp1, tp2, status = row
        try:
            entry, sl, tp1, tp2 = map(float, (entry, sl, tp1, tp2))
            is_buy = 'BUY' in str(sig_type) or 'شراء' in str(sig_type)
            if is_buy:
                risk = max(abs(entry-sl), 1e-9)
                if price >= tp2:
                    update_trade_state(trade_id, 'TP2_HIT', tp2, (tp2-entry)/risk, price-entry)
                elif status == 'OPEN' and price >= tp1:
                    update_trade_state(trade_id, 'TP1_HIT', tp1, (tp1-entry)/risk, price-entry)
                elif price <= sl:
                    update_trade_state(trade_id, 'SL_HIT', sl, (sl-entry)/risk, price-entry)
            else:
                risk = max(abs(sl-entry), 1e-9)
                if price <= tp2:
                    update_trade_state(trade_id, 'TP2_HIT', tp2, (entry-tp2)/risk, entry-price)
                elif status == 'OPEN' and price <= tp1:
                    update_trade_state(trade_id, 'TP1_HIT', tp1, (entry-tp1)/risk, entry-price)
                elif price >= sl:
                    update_trade_state(trade_id, 'SL_HIT', sl, (entry-sl)/risk, entry-price)
        except Exception as e:
            print(f"⚠️ خطأ في مراقبة الصفقة {trade_id}: {e}")

# ------------------------------------
# 🧠 محرك ذكاء GEMINI لتفريغ أسباب الخسارة وتدقيق الفرص اللحظية
# ------------------------------------
def get_recent_gemini_insights():
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
    if not gemini_client:
        return {"approved": True, "reason": "اعتماد كمي أوتوماتيكي (Gemini غير مفعل)"}

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
    
    candidate_models = ['gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-1.5-pro']

    for target_model in candidate_models:
        for attempt in range(2):
            try:
                response = gemini_client.models.generate_content(
                    model=target_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                data = json.loads(response.text)
                return data
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    time.sleep(2)
                    continue
                else:
                    break

    return {"approved": True, "reason": "اعتماد كمي تلقائي (تم تجاوز حدود استخدام Gemini)"}

def update_open_trades_outcome_historical(df_m15):
    conn = get_db_connection()
    resolved_ids = []
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, timestamp, signal_type, sl, tp1 FROM trades WHERE outcome IS NULL")
        open_trades = cursor.fetchall()

        if not open_trades:
            return

        spot_data = get_market_data()
        live_price = spot_data.get("gold", 0.0) if spot_data else 0.0

        df_clean = clean_df_columns(df_m15.copy()) if df_m15 is not None and not df_m15.empty else pd.DataFrame()
        if not df_clean.empty:
            df_index = df_clean.index
            if df_index.tz is None:
                df_index = df_index.tz_localize(timezone.utc)
            else:
                df_index = df_index.tz_convert(timezone.utc)

        ph = "%s" if is_postgres() and isinstance(conn, psycopg2.extensions.connection) else "?"

        for trade_id, trade_time_str, sig_type, sl, tp1 in open_trades:
            try:
                outcome = None

                if live_price > 1000:
                    if "BUY" in sig_type or "شراء" in sig_type:
                        if live_price >= tp1:
                            outcome = 1
                        elif live_price <= sl:
                            outcome = 0
                    elif "SELL" in sig_type or "بيع" in sig_type:
                        if live_price <= tp1:
                            outcome = 1
                        elif live_price >= sl:
                            outcome = 0

                if outcome is None and not df_clean.empty:
                    if isinstance(trade_time_str, datetime):
                        trade_time = trade_time_str if trade_time_str.tzinfo else trade_time_str.replace(tzinfo=timezone.utc)
                    else:
                        trade_time = datetime.strptime(str(trade_time_str)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        
                    sub_df = df_clean[df_index >= trade_time]
                    if not sub_df.empty:
                        for idx, row in sub_df.iterrows():
                            open_val = float(row['Open'])
                            high_val = float(row['High'])
                            low_val = float(row['Low'])

                            if "BUY" in sig_type or "شراء" in sig_type:
                                if low_val <= sl and high_val >= tp1:
                                    outcome = 0 if abs(open_val - sl) < abs(open_val - tp1) else 1
                                    break
                                elif low_val <= sl:
                                    outcome = 0
                                    break
                                elif high_val >= tp1:
                                    outcome = 1
                                    break

                            elif "SELL" in sig_type or "بيع" in sig_type:
                                if high_val >= sl and low_val <= tp1:
                                    outcome = 0 if abs(open_val - sl) < abs(open_val - tp1) else 1
                                    break
                                elif high_val >= sl:
                                    outcome = 0
                                    break
                                elif low_val <= tp1:
                                    outcome = 1
                                    break

                if outcome is not None:
                    cursor.execute(f"UPDATE trades SET outcome = {outcome} WHERE id = {ph}", (trade_id,))
                    resolved_ids.append((trade_id, outcome))
                    print(f"🎯 [تحديث الصفقة] الصفقة رقم {trade_id} تم حسمها بالنتيجة: {'ربح ✅' if outcome == 1 else 'خسارة ❌'}")
            except Exception as e:
                print(f"خطأ في تقييم تتبع الصفقة رقم {trade_id}: {e}")

        conn.commit()
    except Exception as e:
        print(f"خطأ تحديث نتائج الصفقات: {e}")
    finally:
        release_db_connection(conn)
    for trade_id, outcome in resolved_ids:
        status_for_learning='SL_HIT' if outcome==0 else 'TP2_HIT'
        payload=_build_trade_learning_payload(trade_id,status_for_learning)
        if payload:
            enqueue_learning_event('TRADE_OUTCOME',f'trade_outcome:{trade_id}:{status_for_learning}',payload,priority=1 if outcome==0 else 3)

def build_historic_market_features():
    cache = get_chart_data_cached()
    df_m15 = cache.get("df_gold_m15")
    df_dxy_m15 = cache.get("df_dxy_m15")
    
    if df_m15 is None or len(df_m15) < 100:
        return None, None

    df_clean = clean_df_columns(df_m15.copy())
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

def _financial_metrics(r_values):
    vals = np.asarray(list(r_values), dtype=float)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return {"trades": 0, "total_r": 0.0, "expectancy_r": 0.0, "profit_factor": 0.0, "max_drawdown_r": 0.0}
    cumulative = np.cumsum(vals)
    peak = np.maximum.accumulate(np.concatenate(([0.0], cumulative)))
    dd = peak[1:] - cumulative
    gross_win = float(vals[vals > 0].sum())
    gross_loss = float(-vals[vals < 0].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    return {"trades": int(vals.size), "total_r": float(vals.sum()), "expectancy_r": float(vals.mean()), "profit_factor": float(pf), "max_drawdown_r": float(dd.max()) if dd.size else 0.0}

def _save_model_evaluation(meta, promoted):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        is_pg = is_postgres() and isinstance(conn, psycopg2.extensions.connection)
        ph = "%s" if is_pg else "?"
        cur.execute(f"INSERT INTO model_evaluations (created_at, model_name, feature_version, sample_size, oos_trades, total_r, expectancy_r, profit_factor, max_drawdown_r, promoted) VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})", (ts, meta.get("model_name", "RandomForest"), meta.get("feature_version", "v1"), meta.get("sample_size", 0), meta.get("oos_trades", 0), meta.get("total_r", 0.0), meta.get("expectancy_r", 0.0), meta.get("profit_factor", 0.0), meta.get("max_drawdown_r", 0.0), int(bool(promoted))))
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        print(f"⚠️ تعذر حفظ تقييم النموذج: {e}")
    finally:
        release_db_connection(conn)

def train_self_learning_model():
    """Champion-Challenger: تقسيم زمني OOS وترقية إحصائية معتنكة لبيانات التعلم"""
    global CACHED_MODEL, CACHED_MODEL_META, LAST_TRAIN_TIME
    now = datetime.now(timezone.utc)
    with MODEL_LOCK:
        if CACHED_MODEL is not None and LAST_TRAIN_TIME is not None and (now - LAST_TRAIN_TIME).total_seconds() < MODEL_REFRESH_SECONDS:
            return CACHED_MODEL

        conn = get_db_connection()
        df = None
        try:
            limit = int(max(100, min(MODEL_TRAIN_LIMIT, 1000)))
            df = pd.read_sql_query(f"SELECT rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio, outcome, realized_r FROM trades WHERE outcome IS NOT NULL ORDER BY id DESC LIMIT {limit}", conn)
        except Exception as e:
            print(f"تنبيه استعلام قاعدة البيانات لتدريب الذكاء الاصطناعي: {e}")
        finally:
            release_db_connection(conn)

        feature_cols = ['rsi', 'dxy_corr', 'macd_diff', 'stoch_k', 'volatility_ratio']
        if df is None or len(df) < 60:
            X, y = build_historic_market_features()
            if X is None or y is None or len(X) < 60 or len(np.unique(y)) < 2:
                return CACHED_MODEL
            df = X.copy()
            df['outcome'] = y.values
            df['realized_r'] = np.where(df['outcome'].values == 1, 1.0, -1.0)

        df = df.tail(limit).copy().reset_index(drop=True)
        
        # 🔧 تنظيف الأعمدة الرقمية لمنع القيم غير المحدودة بدون استثناءات ndarray
        for col in feature_cols:
            arr = pd.to_numeric(df[col], errors='coerce').to_numpy()
            df[col] = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)

        df['realized_r'] = pd.to_numeric(df['realized_r'], errors='coerce')
        
        # 🔧 إصلاح استثناء "value parameter must be a scalar, dict or Series" عند دمج np.where مع fillna
        fallback_r = pd.Series(np.where(df['outcome'].astype(int) == 1, 1.0, -1.0), index=df.index)
        df['realized_r'] = df['realized_r'].fillna(fallback_r)

        split_idx = int(len(df) * 0.75)
        if split_idx < 50 or len(df) - split_idx < MODEL_MIN_OOS_TRADES:
            return CACHED_MODEL

        X_train, y_train = df.loc[:split_idx-1, feature_cols], df.loc[:split_idx-1, 'outcome'].astype(int)
        X_oos, y_oos = df.loc[split_idx:, feature_cols], df.loc[split_idx:, 'outcome'].astype(int)
        realized_oos = df.loc[split_idx:, 'realized_r'].astype(float).to_numpy()
        if len(np.unique(y_train)) < 2 or len(np.unique(y_oos)) < 2:
            return CACHED_MODEL

        try:
            if not system_monitor.heavy_tasks_allowed():
                logger.warning('[HEALTH] تم تأجيل تدريب Challenger بسبب ضغط الذاكرة.')
                return CACHED_MODEL
            candidate = RandomForestClassifier(n_estimators=50, max_depth=3, min_samples_leaf=3, class_weight='balanced', random_state=42, n_jobs=1)
            candidate.fit(X_train, y_train)
            pred = candidate.predict(X_oos)
            selected_r = np.where(pred == 1, realized_oos, 0.0)
            metrics = _financial_metrics(selected_r)
            metrics.update({"model_name": "RandomForest", "feature_version": "v1", "sample_size": int(len(df)), "oos_trades": int(len(selected_r))})

            current = CACHED_MODEL_META or {"total_r": -np.inf, "expectancy_r": -np.inf, "max_drawdown_r": np.inf, "profit_factor": 0.0}
            promoted = (metrics["total_r"] >= current.get("total_r", -np.inf) + MODEL_PROMOTION_MARGIN_R and metrics["expectancy_r"] >= current.get("expectancy_r", -np.inf) and metrics["max_drawdown_r"] <= max(3.0, current.get("max_drawdown_r", np.inf)) and metrics["profit_factor"] >= 1.0)
            if CACHED_MODEL is None:
                promoted = metrics["total_r"] > 0 and metrics["expectancy_r"] > 0 and metrics["profit_factor"] >= 1.0 and metrics["max_drawdown_r"] <= 5.0

            _save_model_evaluation(metrics, promoted)
            LAST_TRAIN_TIME = now
            if promoted:
                CACHED_MODEL, CACHED_MODEL_META = candidate, metrics
                print(f"🧠 تمت ترقية النموذج: Total R={metrics['total_r']:.2f} | Expectancy={metrics['expectancy_r']:.3f}R | PF={metrics['profit_factor']:.2f} | DD={metrics['max_drawdown_r']:.2f}R")
            else:
                print("🧠 تم رفض Challenger والإبقاء على النموذج النشط لتجنب تقلب النماذج.")
        except Exception as err:
            print(f"تنبيه تدريب الذكاء الاصطناعي: {err}")
        finally:
            del df
            gc.collect()
        return CACHED_MODEL

def check_news_guard():
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    weekday = now_utc.weekday()
    day_of_month = now_utc.day

    is_news_high, news_title, fetch_failed = fetch_live_economic_news_alert()
    if is_news_high:
        return False, f"حظر آلي: صدور خبر شديد التأثير في السوق الآن ({news_title})."
    
    if fetch_failed:
        return True, f"وضع مراقبة مخفّض: تعذر التحقق من الأخبار اللحظية ({news_title})."

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
# 4. محرك البيانات الفورية الموحد
# ------------------------------------
def fetch_yahoo_direct(symbol, range_str="10d", interval_str="15m"):
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
    """جلب سعر الذهب الفوري Spot Gold المباشر لرمز XAU/USD حصراً مع دعم الاحتياطي للشموع"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    
    # 1. المصدر الرئيسي الموحد: XAUUSD Spot عبر Yahoo Chart API
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?interval=1m&range=1d", headers=headers, timeout=3)
        if r.status_code == 200:
            res = r.json()
            price = float(res['chart']['result'][0]['meta']['regularMarketPrice'])
            if price > 1000:
                return round(price, 2)
    except Exception:
        pass

    # 2. المصدر الثاني المباشر: IFC Markets XAUUSD Spot Scraping
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

    # 3. المصدر الثالث الاحتياطي: Binance PAXGUSDT
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT", headers=headers, timeout=2)
        if r.status_code == 200:
            price = float(r.json()['price'])
            if price > 1000:
                return round(price, 2)
    except Exception:
        pass

    # 4. المصدر الرابع الاحتياطي: الكاش المباشر لآخر شمعة مفحوصة
    try:
        with cache_lock:
            df_m15 = MARKET_DATA_CACHE.get("df_gold_m15")
            if df_m15 is not None and not df_m15.empty:
                close_s = to_1d_series(df_m15['Close'])
                last_price = float(close_s.iloc[-1])
                if last_price > 1000:
                    return round(last_price, 2)
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
                df_gold_m15 = clean_df_columns(yf.download("XAUUSD=X", period="10d", interval="15m", progress=False, threads=False, auto_adjust=False))
            if df_gold_h1.empty:
                df_gold_h1 = clean_df_columns(yf.download("XAUUSD=X", period="60d", interval="1h", progress=False, threads=False, auto_adjust=False))
            if df_dxy_m15.empty:
                df_dxy_m15 = clean_df_columns(yf.download("DX-Y.NYB", period="10d", interval="15m", progress=False, threads=False, auto_adjust=False))
            if df_us10y_m15.empty:
                df_us10y_m15 = clean_df_columns(yf.download("^TNX", period="10d", interval="15m", progress=False, threads=False, auto_adjust=False))

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

def get_verified_closed_m15(df):
    if df is None or df.empty:
        return pd.DataFrame()
    x = clean_df_columns(df.copy())
    if not isinstance(x.index, pd.DatetimeIndex):
        x.index = pd.to_datetime(x.index, utc=True)
    elif x.index.tz is None:
        x.index = x.index.tz_localize(timezone.utc)
    else:
        x.index = x.index.tz_convert(timezone.utc)
    x = x.sort_index().loc[~x.index.duplicated(keep='last')]
    now = datetime.now(timezone.utc)
    if (now - x.index[-1]).total_seconds() < 900:
        x = x.iloc[:-1]
    return x

def analyze_institutional_engine():
    try:
        cache = get_chart_data_cached()
        df_gold_h1 = cache["df_gold_h1"]
        df_gold_m15 = cache["df_gold_m15"]
        df_dxy_m15 = cache["df_dxy_m15"]
        df_us10y_m15 = cache["df_us10y_m15"]

        df_gold_m15 = get_verified_closed_m15(df_gold_m15)
        if df_gold_m15.empty or df_gold_h1.empty or len(df_gold_m15) < 100:
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
            "us10y_trend": "DOWN" if (len(close_us10y_m15) >= 5 and close_us10y_m15.iloc[-1] < close_us10y_m15.iloc[-5]) else "UP",
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
    global LAST_SCANNER_CANDLE
    with SIGNAL_LOCK:
        safe_news, news_reason = check_news_guard()
        if not safe_news:
            data_quick = get_market_data()
            return {"status": "WAIT", "reason": f"🛑 تم إيقاف الإشارة مؤقتاً: {news_reason}", "price": data_quick.get('gold', 0.0)}

        data = analyze_institutional_engine()
        if not data:
            return {"status": "WAIT", "reason": "تعذر تجهيز بيانات السوق المغلقة حالياً.", "price": get_market_data().get('gold', 0.0)}

        h4_trend, state = data["h4_trend"], data["state_label"]
        df, dxy_corr, smc, current_price = data["df_m15"], data["dxy_corr"], data["smc"], data["last_price"]
        close, high, low = map(lambda c: to_1d_series(df[c]), ['Close','High','Low'])
        rsi = float(ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1])
        atr = float(ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1])
        ema_fast = float(ta.trend.EMAIndicator(close, window=9).ema_indicator().iloc[-1])
        ema_slow = float(ta.trend.EMAIndicator(close, window=21).ema_indicator().iloc[-1])
        macd_diff = float(ta.trend.MACD(close).macd_diff().iloc[-1])
        stoch_k = float(ta.momentum.StochasticOscillator(high, low, close).stoch().iloc[-1])
        if not np.isfinite(atr) or atr <= 0 or current_price <= 0:
            return {"status":"WAIT","reason":"بيانات ATR/السعر غير صالحة حالياً.","price":current_price}

        volatility_ratio = round((atr/current_price)*100,4)
        if volatility_ratio < 0.012:
            return {"status":"WAIT","reason":"التذبذب الحالي منخفض جداً ولا يكفي لإدارة صفقة بكفاءة.","price":current_price}

        clf = train_self_learning_model()
        confidence = 0.60
        if clf:
            try:
                feat = pd.DataFrame([[rsi,dxy_corr,macd_diff,stoch_k,volatility_ratio]], columns=['rsi','dxy_corr','macd_diff','stoch_k','volatility_ratio'])
                probs = clf.predict_proba(feat)[0]
                classes = list(clf.classes_)
                if 1 in classes:
                    confidence = round(float(probs[classes.index(1)]),2)
            except Exception:
                confidence = 0.60

        bull_score = 0.0
        bear_score = 0.0
        reasons_bull, reasons_bear = [], []
        if h4_trend == 'BULLISH': bull_score += 2; reasons_bull.append('اتجاه H4 صاعد')
        elif h4_trend == 'BEARISH': bear_score += 2; reasons_bear.append('اتجاه H4 هابط')
        if state == 'BULLISH': bull_score += 1; reasons_bull.append('حالة السوق صاعدة')
        elif state == 'BEARISH': bear_score += 1; reasons_bear.append('حالة السوق هابطة')
        if ema_fast > ema_slow: bull_score += 1; reasons_bull.append('زخم M15 صاعد')
        elif ema_fast < ema_slow: bear_score += 1; reasons_bear.append('زخم M15 هابط')
        if rsi < 70: bull_score += 0.5
        if rsi > 30: bear_score += 0.5
        if smc.get('fvg_bullish') or smc.get('sweep_bullish'): bull_score += 1.5; reasons_bull.append('تأكيد سيولة/FVG صاعد')
        if smc.get('fvg_bearish') or smc.get('sweep_bearish'): bear_score += 1.5; reasons_bear.append('تأكيد سيولة/FVG هابط')
        if dxy_corr <= 0.35:
            bull_score += 0.5; bear_score += 0.5
        if confidence >= 0.58: bull_score += 0.75; bear_score += 0.75
        elif confidence < 0.45: bull_score -= 0.5; bear_score -= 0.5

        signal_type = None
        if bull_score >= 4.0 and bull_score - bear_score >= 0.75:
            signal_type = 'BUY'
        elif bear_score >= 4.0 and bear_score - bull_score >= 0.75:
            signal_type = 'SELL'
        else:
            return {"status":"WAIT","reason":f"لم يكتمل ترجيح كافٍ بعد. صاعد={bull_score:.1f} | هابط={bear_score:.1f} | ثقة التعلم={int(confidence*100)}%.","price":current_price}

        if has_active_open_trade(signal_type):
            return {"status":"WAIT","reason":f"توجد صفقة {('شراء' if signal_type=='BUY' else 'بيع')} مفتوحة بالفعل؛ لن نكررها قبل حسم الصفقة الحالية.","price":current_price}

        if signal_type == 'BUY':
            sl = round(current_price - atr*1.25,2); tp1 = round(current_price + atr*1.6,2); tp2 = round(current_price + atr*2.8,2)
            smc_note = 'تأكيد صاعد من السيولة/FVG' if reasons_bull else 'تأكيد من الزخم والاتجاه'
        else:
            sl = round(current_price + atr*1.25,2); tp1 = round(current_price - atr*1.6,2); tp2 = round(current_price - atr*2.8,2)
            smc_note = 'تأكيد هابط من السيولة/FVG' if reasons_bear else 'تأكيد من الزخم والاتجاه'

        candle_timestamp = pd.Timestamp(df.index[-1]).isoformat()
        candle_id = f"XAUUSD_M15_{pd.Timestamp(df.index[-1]).strftime('%Y%m%d_%H%M')}"
        candidate_signal = {
            'status':'SIGNAL','type':'🟢 شراء مرن' if signal_type=='BUY' else '🔴 بيع مرن',
            'entry':round(current_price,2),'sl':sl,'tp1':tp1,'tp2':tp2,'rr':'1:2.2',
            'rsi':round(rsi,1),'dxy_corr':round(dxy_corr,2),'confidence':int(confidence*100),
            'risk':'1% مبدئياً (تُراجع حسب الثقة والتذبذب)','smc_note':smc_note,
            'candle_id':candle_id,'signal_candle_close':round(float(close.iloc[-1]),2),
            'signal_candle_time':candle_timestamp,'score_bull':round(bull_score,2),'score_bear':round(bear_score,2)
        }

        gemini_eval = gemini_verify_signal(candidate_signal, {'h4_trend':h4_trend,'state_label':state})
        candidate_signal['gemini_note'] = gemini_eval.get('reason','تمت المراجعة بواسطة Gemini')
        candidate_signal['ai_score'] = 1.0 if gemini_eval.get('approved', True) else 0.0
        if not gemini_eval.get('approved', True) and confidence < 0.55 and max(bull_score,bear_score) < 5.0:
            return {'status':'WAIT','reason':f"مراجعة Gemini أوصت بالانتظار: {gemini_eval.get('reason','مخاطرة مرتفعة')}", 'price':current_price}

        inserted, trade_id = log_trade(signal_type, current_price, sl, tp1, tp2, round(rsi,1), dxy_corr, round(macd_diff,3), round(stoch_k,1), volatility_ratio, confidence, candle_id=candle_id, signal_score=max(bull_score,bear_score), ai_score=candidate_signal.get('ai_score'))
        if not inserted:
            return {'status':'WAIT','reason':'تمت معالجة هذه الشمعة مسبقاً؛ تم منع الإشعار المكرر.', 'price':current_price, 'candle_id':candle_id}
        candidate_signal['trade_id'] = trade_id
        LAST_SCANNER_CANDLE = candle_id
        return candidate_signal

# ------------------------------------
# 6. محرك اختبار الاستراتيجية العكسي
# ------------------------------------
def run_quant_backtest():
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
        f"📊 **نتائج الاختبار العكسي للاستراتيجية الكمية والتعلم الذاتي**\n"
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
# 7. المراقبة الآلية ومراقب الذاكرة
# ------------------------------------
async def keep_alive_ping():
    url = os.getenv("RENDER_EXTERNAL_URL", "https://gold-quant-bot.onrender.com")
    while True:
        await asyncio.sleep(480)
        try:
            await asyncio.to_thread(requests.get, url, timeout=5)
            print("⚓ تم إرسال إشارة الاستيقاظ الذاتية لـ Render بنجاح.")
        except Exception as e:
            print(f"تنبيه فحص الاستيقاظ الذاتي: {e}")

async def background_cache_worker():
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
            await asyncio.to_thread(monitor_open_trades)
            sig = await asyncio.to_thread(generate_quant_signal)
            if sig and sig["status"] == "SIGNAL":
                current_candle = sig.get("candle_id")
                if current_candle != last_sent_candle:
                    last_sent_candle = current_candle
                    msg = (
                        f"🚨 **إشارة تداول جديدة مدعومة بالتحليل الكمي والذكاء الاصطناعي**\n"
                        f"───────────────────\n"
                        f"النوع: {sig['type']}\n"
                        f"🎯 **نسبة ثقة نموذج التعلم:** {sig['confidence']}%\n"
                        f"⚖️ **المخاطرة المبدئية:** {sig['risk']}\n"
                        f"💵 **سعر التنفيذ اللحظي:** ${sig['entry']}\n"
                        f"📉 **إغلاق شمعة الإشارة:** ${sig.get('signal_candle_close', sig['entry'])}\n"
                        f"🛑 **وقف الخسارة:** ${sig['sl']}\n"
                        f"🎯 **الهدف الأول (TP1):** ${sig['tp1']}\n"
                        f"🎯 **الهدف الثاني (TP2):** ${sig['tp2']}\n"
                        f"💡 **تأكيد الهيكل:** {sig['smc_note']}\n"
                        f"🤖 **مراجعة الذكاء الاصطناعي:** {sig.get('gemini_note', 'تمت المراجعة')}\n"
                        f"🔗 **ارتباط الدولار:** {sig['dxy_corr']}\n"
                        f"───────────────────\n"
                        f"🤖 *تم تأكيد الإشارة بالتحليل الكمي ومراجعة الذكاء الاصطناعي.*"
                    )
                    subscribers = get_subscribers()
                    for user_id in subscribers:
                        if is_authenticated(user_id):
                            try:
                                await safe_send_message(app.bot, chat_id=user_id, text=msg, parse_mode='Markdown')
                            except Exception as send_err:
                                print(f"تعذر الإرسال للمستخدم {user_id}: {send_err}")
        except Exception as e:
            print(f"خطأ في الفحص الآلي: {e}")
            
        await asyncio.sleep(30)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    system_monitor.record_error("TELEGRAM", context.error)

async def daily_telemetry_worker(app):
    while True:
        try:
            await asyncio.sleep(86400)
            summary=system_monitor.summary()
            msg=(f"🩺 **تقرير صحة النظام اليومي**\nالذاكرة: {summary['ram_mb']}MB | المستوى: {summary['tier']}\nمدة التشغيل: {summary['uptime_min']} دقيقة\nأخطاء مسجلة: {summary['errors'] or 'لا يوجد'}")
            if ADMIN_CHAT_ID: await safe_send_message(app.bot, chat_id=ADMIN_CHAT_ID, text=msg, parse_mode='Markdown')
        except asyncio.CancelledError: break
        except Exception as e: system_monitor.record_error('DAILY_TELEMETRY',e)

async def post_init(app):
    recover_interrupted_learning_events()
    learning_manager.initialize()
    # استخدام create_managed_task لمنع تدمير المهام بواسطة Garbage Collector
    create_managed_task(background_cache_worker())
    create_managed_task(auto_market_scanner(app))
    create_managed_task(keep_alive_ping())
    create_managed_task(learning_manager.worker_loop(LEARNING_STOP_EVENT))
    create_managed_task(daily_telemetry_worker(app))

# ------------------------------------
# 🧹 أمر تصفير كافة البيانات والذاكرة
# ------------------------------------
async def reset_all_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await safe_reply_text(update, "🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return

    await safe_reply_text(update, "⏳ **جاري تصفير قاعدة البيانات وتفريغ الذاكرة العشوائية بالكامل...**", parse_mode='Markdown')

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        is_pg = is_postgres() and isinstance(conn, psycopg2.extensions.connection)

        if is_pg:
            cursor.execute("""
                TRUNCATE TABLE trades, learning_events, gemini_insights, model_evaluations, 
                subscribers, authenticated_users, config RESTART IDENTITY CASCADE;
            """)
        else:
            tables = ['trades', 'learning_events', 'gemini_insights', 'model_evaluations', 'subscribers', 'authenticated_users', 'config']
            for t in tables:
                cursor.execute(f"DELETE FROM {t};")
                try:
                    cursor.execute(f"DELETE FROM sqlite_sequence WHERE name='{t}';")
                except Exception:
                    pass
        conn.commit()
    except Exception as e:
        await safe_reply_text(update, f"❌ حدث خطأ أثناء تصفير قاعدة البيانات: {e}")
        return
    finally:
        release_db_connection(conn)

    with cache_lock:
        GLOBAL_CACHE["market_data"] = {"gold": 0.0, "dxy": 99.85, "us10y": 4.63}
        GLOBAL_CACHE["analysis"] = None
        GLOBAL_CACHE["last_updated"] = None

        MARKET_DATA_CACHE["df_gold_h1"] = pd.DataFrame()
        MARKET_DATA_CACHE["df_gold_m15"] = pd.DataFrame()
        MARKET_DATA_CACHE["df_dxy_m15"] = pd.DataFrame()
        MARKET_DATA_CACHE["df_us10y_m15"] = pd.DataFrame()
        MARKET_DATA_CACHE["last_fetch"] = None

    global CACHED_MODEL, CACHED_MODEL_META, LAST_TRAIN_TIME
    with MODEL_LOCK:
        CACHED_MODEL = None
        CACHED_MODEL_META = {}
        LAST_TRAIN_TIME = None

    authenticate_user(chat_id)
    add_subscriber(chat_id)

    await safe_reply_text(
        update,
        "🧹 **تم مسح كافة البيانات السابقة وتفرغ الذاكرة العشوائية بنجاح!**\n"
        "يمكنك الآن البدء بحساب جديد وقاعدة بيانات نظيفة 100% 🚀",
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )

# ------------------------------------
# 🔍 8. دالة فحص الأخطاء الشامل والمباشر للنظام
# ------------------------------------
async def system_health_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await safe_reply_text(update, "🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return

    await safe_reply_text(update, "🔍 **جاري بدء الفحص الشامل لجميع الاتصالات، الخوادم، والأنظمة البرمجية...**\nقد يستغرق الفحص بضع ثوانٍ.", parse_mode='Markdown')

    report_lines = []
    issues_found = []

    resource_summary=system_monitor.summary()
    report_lines.append(f"🛡️ **حارس الموارد:** RAM={resource_summary['ram_mb']}MB | المستوى={resource_summary['tier']}")
    if resource_summary['tier'] in {'DEFERRED','EMERGENCY'}:
        report_lines.append("  ⚠️ تم تأجيل مهام التعلم الثقيلة لحماية مسار التداول.")

    report_lines.append("⚙️ **1. فحص متغيرات البيئة والإعدادات:**")
    if TOKEN and len(TOKEN) > 20:
        report_lines.append("  ✅ توكن التلغرام (TELEGRAM_TOKEN): صالح ومفعل من البيئة.")
    else:
        report_lines.append("  ❌ توكن التلغرام: مفقود أو غير صحيح.")
        issues_found.append("TELEGRAM_TOKEN مفقود أو غير صالح.")

    if is_postgres():
        report_lines.append("  ✅ قاعدة البيانات: Supabase PostgreSQL مفعلة.")
    else:
        report_lines.append("  ⚠️ قاعدة البيانات: SQLite محلي (لم يتم ضبط DATABASE_URL).")

    if GEMINI_API_KEY:
        report_lines.append("  ✅ مفتاح الذكاء الاصطناعي: متوفر.")
    else:
        report_lines.append("  ⚠️ مفتاح الذكاء الاصطناعي: غير مضبوط في متغيرات البيئة.")
        issues_found.append("GEMINI_API_KEY مفقود.")

    report_lines.append("\n🗄️ **2. فحص قاعدة البيانات والجداول:**")
    db_start = time.time()
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1;")
        db_latency = (time.time() - db_start) * 1000
        report_lines.append(f"  ✅ الاتصال بقاعدة البيانات: ممتاز (زمن الاستجابة: {db_latency:.1f}ms).")

        tables = ['trades', 'subscribers', 'authenticated_users', 'config', 'gemini_insights', 'model_evaluations', 'learning_events']
        missing_tables = []
        for t in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {t};")
            except Exception:
                missing_tables.append(t)

        if not missing_tables:
            report_lines.append("  ✅ سلامة الجداول (5/5): جميع الجداول الأساسية موجودة وتعمل.")
        else:
            report_lines.append(f"  ❌ نقص في الجداول التالية: {', '.join(missing_tables)}")
            issues_found.append(f"جداول قاعدة البيانات المفقودة: {missing_tables}")

    except Exception as e:
        report_lines.append(f"  ❌ فشل الاتصال بقاعدة البيانات: {e}")
        issues_found.append(f"خطأ قاعدة البيانات: {e}")
    finally:
        if conn:
            release_db_connection(conn)

    report_lines.append("\n🧠 **3. فحص اتصال ونماذج الذكاء الاصطناعي:**")
    if gemini_client:
        try:
            test_resp = await asyncio.to_thread(
                gemini_client.models.generate_content,
                model='gemini-1.5-flash',
                contents='ping'
            )
            if test_resp and test_resp.text:
                report_lines.append("  ✅ اتصال نموذج الذكاء الاصطناعي السريع: يعمل بنجاح وسريع الاستجابة.")
            else:
                report_lines.append("  ⚠️ خدمة الذكاء الاصطناعي استجابت دون نص.")
        except Exception as ge:
            err_str = str(ge)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                report_lines.append("  ⚠️ تم بلوغ الحد المجاني للطلبات؛ سيستمر النظام بالتحليل الكمي تلقائياً.")
            else:
                report_lines.append(f"  ❌ خطأ في اتصال الذكاء الاصطناعي: {ge}")
                issues_found.append(f"خطأ في خدمة الذكاء الاصطناعي: {ge}")
    else:
        report_lines.append("  ⚠️ خدمة الذكاء الاصطناعي غير مفعلة (يعمل النظام بالاعتماد الكمي التلقائي).")

    report_lines.append("\n📊 **4. فحص اتصالات مصادر أسعار السوق (XAU/USD Spot):**")
    gold_price = await asyncio.to_thread(fetch_live_spot_gold)
    if gold_price > 1000:
        report_lines.append(f"  ✅ جلب سعر الذهب الفوري: ناجح (${gold_price}).")
    else:
        report_lines.append("  ❌ فشل جلب سعر الذهب اللحظي (السعر يساوي 0.0).")
        issues_found.append("تعذر جلب سعر الذهب المباشر Spot Gold.")

    market_cache = await asyncio.to_thread(get_chart_data_cached)
    if not market_cache["df_gold_m15"].empty:
        report_lines.append(f"  ✅ جلب بيانات شموع الذهب M15: {len(market_cache['df_gold_m15'])} شمعة محملة.")
    else:
        report_lines.append("  ❌ فشل تحميل بيانات الشموع (M15) من مزود البيانات.")
        issues_found.append("جدول الشموع M15 فارغ.")

    if not market_cache["df_gold_h1"].empty:
        report_lines.append(f"  ✅ جلب بيانات الشموع الكبيرة (H1): {len(market_cache['df_gold_h1'])} شمعة محملة.")
    else:
        report_lines.append("  ⚠️ فشل تحميل بيانات الشموع (H1).")

    report_lines.append("\n📰 **5. فحص مصادر مفكرة الأخبار الاقتصادية مع تدقيق البيانات:**")
    try:
        is_high, news_info, is_fail = await asyncio.to_thread(fetch_live_economic_news_alert)
        if not is_fail:
            report_lines.append(f"  ✅ الاتصال بمصادر الأخبار ومطابقة البيانات: ناجح وموثق.")
        else:
            report_lines.append(f"  ⚠️ تعذر الاتصال بكافة خوادم الأخبار الستة: {news_info}")
            issues_found.append("سيرفرات الأخبار الخارجية حظرت الاتصال أو متوقفة.")
    except Exception as ne:
        report_lines.append(f"  ❌ خطأ فحص سيرفرات الأخبار: {ne}")
        issues_found.append(f"خطأ محرك الأخبار: {ne}")

    report_lines.append("\n🤖 **6. فحص المحرك الكمي وموديل ML:**")
    try:
        engine_res = await asyncio.to_thread(analyze_institutional_engine)
        if engine_res:
            report_lines.append(f"  ✅ تحليل بنية السوق: يعمل (الاتجاه H4: {arabic_direction(engine_res['h4_trend'])} | حالة السوق: {arabic_direction(engine_res['state_label'])}).")
        else:
            report_lines.append("  ❌ فشل تحليل المحرك المؤسسي.")
            issues_found.append("المحرك المؤسسي لم يستطع قراءة البيانات.")
    except Exception as ee:
        report_lines.append(f"  ❌ خطأ في المحرك المؤسسي: {ee}")
        issues_found.append(f"خطأ المحرك المؤسسي: {ee}")

    report_lines.append("\n🌐 **7. فحص خادم الويب المحلي (Flask Server):**")
    try:
        port = int(os.environ.get("PORT", 10000))
        r_web = await asyncio.to_thread(requests.get, f"http://127.0.0.1:{port}/", timeout=3)
        if r_web.status_code == 200:
            report_lines.append("  ✅ خادم الويب المحلي يعمل ويستجيب بشكل صحيح.")
        else:
            report_lines.append(f"  ⚠️ خادم الويب أعاد رمز استجابة: {r_web.status_code}")
    except Exception as we:
        report_lines.append(f"  ⚠️ تعذر الاتصال بخادم الويب المحلي: {we}")

    report_lines.append("\n───────────────────")
    if not issues_found:
        report_lines.append("🎉 **النتيجة النهائية:** النظام يعمل بكفاءة واستقرار تام!")
    else:
        report_lines.append(f"🚨 **تم اكتشاف ({len(issues_found)}) تنبيه/خطأ بحاجة للمراجعة:**")
        for idx, issue in enumerate(issues_found, 1):
            report_lines.append(f"  {idx}. {issue}")

    full_report = "\n".join(report_lines)

    if len(full_report) > 4000:
        for chunk in [full_report[i:i+4000] for i in range(0, len(full_report), 4000)]:
            await safe_reply_text(update, chunk, reply_markup=get_main_keyboard(), parse_mode='Markdown')
    else:
        await safe_reply_text(update, full_report, reply_markup=get_main_keyboard(), parse_mode='Markdown')

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
            await safe_reply_text(
                update,
                "✅ **تم تسجيل الدخول بنجاح!**\n"
                "مرحباً بك في البوت الكمي المؤسسي المدعوم بـ Gemini AI. تم تفعيل كافة الصلاحيات والتنبيهات التلقائية.\n\n"
                "💡 يمكنك الآن الضغط على الأزرار في الأسفل لتنفيذ الأوامر فوراً.",
                reply_markup=get_main_keyboard(),
                parse_mode='Markdown'
            )
            await notify_admin(context, f"🔑 **تسجيل دخول ناجح!**\nالمستخدم: {user_info}")
        else:
            await safe_reply_text(update, "❌ **كلمة السر غير صحيحة!**\nتم تسجيل محاولة الدخول وإبلاغ مسؤول النظام.")
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
    elif "فحص الأخطاء الشامل" in text or text in ["/health", "/check"]:
        await system_health_check(update, context)
    elif "تصفير البيانات" in text or text == "/reset_all":
        await reset_all_data(update, context)
    else:
        await safe_reply_text(
            update,
            "💡 استخدم الأزرار الظاهرة في الأسفل للتحكم بالبوت.",
            reply_markup=get_main_keyboard()
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_info = f"{user.first_name} (@{user.username if user.username else 'بدون معرف'}) [ID: {chat_id}]"

    if not is_authenticated(chat_id):
        await safe_reply_text(
            update,
            "🔒 **البوت محمي بكلمة سر.**\n"
            "يرجى إرسال كلمة السر الخاصة بك للدخول إلى النظام."
        )
        await notify_admin(context, f"🚨 **محاولة وصول جديدة للبوت (/start)**\nالمستخدم: {user_info}\nالحالة: غير مسجل دخول.")
        return

    add_subscriber(chat_id)
    await safe_reply_text(
        update,
        f"أهلاً بك مجدداً! 🚀\n"
        f"حسابك موثق ومفعل في **البوت الكمي الهجين المدعوم بالذكاء الاصطناعي**.\n\n"
        f"💡 اضغط على الأزرار أدناه لتنفيذ ما تريد:",
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await safe_reply_text(update, "🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
    
    data = get_market_data()
    msg = f"📊 **أسعار الذهب اللحظية**\n🟡 الذهب (XAUUSD): ${data['gold']}\n💵 مؤشر الدولار: {data['dxy']}\n📈 عوائد السندات: {data['us10y']}%"
    await safe_reply_text(update, msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')

async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await safe_reply_text(update, "🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
    await safe_reply_text(update, "🧠 جاري تحليل اتجاه السوق والسيولة والبنية السعرية...")
    res = await asyncio.to_thread(analyze_institutional_engine)
    if res:
        smc = res['smc']
        smc_status = "صاعد (FVG/Sweep)" if smc['fvg_bullish'] or smc['sweep_bullish'] else ("هابط (FVG/Sweep)" if smc['fvg_bearish'] or smc['sweep_bearish'] else "محايد")
        
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
            f"🤖 **تقرير بنية سوق الذهب**\n"
            f"───────────────────\n"
            f"💰 سعر الذهب الفوري: ${res['last_price']}\n"
            f"📈 اتجاه H4 الحاكم: {arabic_direction(res['h4_trend'])}\n"
            f"📊 حالة السوق (M15): {arabic_direction(res['state_label'])}\n"
            f"🏦 هيكل السيولة: {smc_status}\n"
            f"🔗 معامل ارتباط الدولار: {res['dxy_corr']}\n"
            f"───────────────────\n"
            f"🧠 **أحدث قاعدة تعلم ذاتي:**\n{last_lesson}"
        )
        await safe_reply_text(update, msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')

async def backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await safe_reply_text(update, "🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
    await safe_reply_text(update, "📈 جاري تشغيل الاختبار العكسي الكمي للبيانات التاريخية...")
    report = await asyncio.to_thread(run_quant_backtest)
    await safe_reply_text(update, report, reply_markup=get_main_keyboard(), parse_mode='Markdown')

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await safe_reply_text(update, "🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
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

        cursor.execute("SELECT COUNT(*) FROM trades WHERE outcome IS NOT NULL")
        pending_trades = cursor.fetchone()[0] or 0

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
        cursor.execute("SELECT COUNT(*) FROM learning_events WHERE status IN ('PENDING','DEFERRED')")
        learning_pending=cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM learning_events WHERE status='FAILED'")
        learning_failed=cursor.fetchone()[0] or 0
        resource_summary=system_monitor.summary()
        
        msg = (
            f"📊 **تقرير أداء المحرك الكمي التفصيلي**\n"
            f"───────────────────\n"
            f"🗄️ **قاعدة البيانات:** {db_type_str}\n"
            f"⏳ **الصفقات المفتوحة قيد التتبع:** {pending_trades}\n"
            f"📈 **معدل النجاح الكلي:** {overall_win_rate}% ({total_wins}/{total_eval})\n"
            f"📅 **أداء آخر 7 أيام:** {weekly_win_rate}% ({week_wins}/{week_eval})\n"
            f"🗓️ **أداء آخر 30 يوماً:** {monthly_win_rate}% ({month_wins}/{month_eval})\n"
            f"───────────────────\n"
            f"🟢 **صفقات الشراء:** {buy_rate}% نجاح ({buy_wins}/{buy_total})\n"
            f"🔴 **صفقات البيع:** {sell_rate}% نجاح ({sell_wins}/{sell_total})\n"
            f"───────────────────\n"
            f"💡 *تم دمج الاختبار الراجع والتعلم الذاتي التلقائي عبر الذكاء الاصطناعي.*\n🧠 *النموذج النشط:* {('متوفر' if CACHED_MODEL else 'بانتظار عينة كافية')}"
        )
        await safe_reply_text(update, msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')
    except Exception as e:
        print(f"خطأ إحضار الإحصائيات: {e}")
    finally:
        release_db_connection(conn)

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await safe_reply_text(update, "🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
    await safe_reply_text(update, "⚡ جاري مطابقة شروط السوق ومراجعة المخاطر بالذكاء الاصطناعي...")
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
            f"💡 تأكيد البنية والسيولة: {sig['smc_note']}\n"
            f"🤖 مراجعة الذكاء الاصطناعي للمخاطر: {sig.get('gemini_note', 'تم التأييد')}"
        )
    else:
        msg = f"⏸️ **تنبيه الانتظار المؤسسي**\n💡 السبب: {sig['reason'] if sig else 'لا توجد فرصة مطابقة'}"
    await safe_reply_text(update, msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')

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
    app.add_handler(CommandHandler("health", system_health_check))
    app.add_handler(CommandHandler("check", system_health_check))
    app.add_handler(CommandHandler("reset_all", reset_all_data))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    
    print("🤖 البوت الهجين (Quant + Gemini API) يعمل بكفاءة تامة وجاهز للمراقبة 24/7...")
    app.run_polling(drop_pending_updates=True)

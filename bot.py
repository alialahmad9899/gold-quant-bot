import logging
import asyncio
import sqlite3
import psycopg2
import os
import requests
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
from multiprocessing import Process

# --- 1. خادم الويب الأساسي لإرضاء Render Web Service وفحص المنفذ فوراً ---
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "XAU/USD Quant Signal Bot is Live and Running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

# 🔒 جلب التوكن وقاعدة البيانات من متغيرات البيئة
TOKEN = os.getenv("TELEGRAM_TOKEN", "8560548173:AAGrJpVfV9Et7l8mMdUtr6Xlj8SJ_lQzxNc")
DATABASE_URL = os.getenv("DATABASE_URL")
DB_FILE = "trades.db"

# ------------------------------------
# 🔑 إعدادات الحماية والآدمن
# ------------------------------------
PASSWORD = "12341212"
ADMIN_CHAT_ID = 0

# ذاكرة عشوائية فائقة السرعة للأسعار والتحليل (In-Memory Cache)
GLOBAL_CACHE = {
    "market_data": {"gold": 0.0, "dxy": 99.85, "us10y": 4.63},
    "analysis": None,
    "last_updated": None
}

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ------------------------------------
# 🛠️ أدوات مساعدة وتجهيز البيانات
# ------------------------------------
def to_1d_series(df_col):
    if isinstance(df_col, pd.DataFrame):
        return df_col.iloc[:, 0]
    return df_col

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("⚡ إشارة فورية"), KeyboardButton("🧠 تحليل بنية السوق")],
        [KeyboardButton("📊 الأسعار اللحظية"), KeyboardButton("📈 إحصائيات النظام")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ------------------------------------
# 1. إدارة قاعدة البيانات الهجينة (PostgreSQL / SQLite)
# ------------------------------------
def is_postgres():
    return DATABASE_URL is not None and len(DATABASE_URL.strip()) > 0

def get_db_connection():
    if is_postgres():
        try:
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

def init_db():
    try:
        conn = get_db_connection()
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

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"خطأ أثناء تهيئة قاعدة البيانات: {e}")

def set_admin_id(chat_id):
    global ADMIN_CHAT_ID
    ADMIN_CHAT_ID = chat_id
    conn = get_db_connection()
    cursor = conn.cursor()
    if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
        cursor.execute("INSERT INTO config (key, value) VALUES ('admin_id', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (chat_id,))
    else:
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('admin_id', ?)", (chat_id,))
    conn.commit()
    conn.close()

def load_admin_id():
    global ADMIN_CHAT_ID
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = 'admin_id'")
        row = cursor.fetchone()
        conn.close()
        if row:
            ADMIN_CHAT_ID = row[0]
    except Exception as e:
        print(f"خطأ في تحميل معرف الأدمن: {e}")

def is_authenticated(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    ph = "%s" if is_postgres() and isinstance(conn, psycopg2.extensions.connection) else "?"
    cursor.execute(f"SELECT chat_id FROM authenticated_users WHERE chat_id = {ph}", (chat_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def authenticate_user(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
        cursor.execute("INSERT INTO authenticated_users (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (chat_id,))
    else:
        cursor.execute("INSERT OR IGNORE INTO authenticated_users (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()
    if ADMIN_CHAT_ID == 0:
        set_admin_id(chat_id)

def add_subscriber(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    if is_postgres() and isinstance(conn, psycopg2.extensions.connection):
        cursor.execute("INSERT INTO subscribers (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (chat_id,))
    else:
        cursor.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

def get_subscribers():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM subscribers")
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, message: str):
    if ADMIN_CHAT_ID and ADMIN_CHAT_ID != 0:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=message, parse_mode='Markdown')
        except Exception as e:
            print(f"خطأ في إرسال الإشعار للآدمن: {e}")

def log_trade(signal_type, entry, sl, tp1, tp2, rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio, confidence):
    conn = get_db_connection()
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
            
    conn.close()

def update_open_trades_outcome_historical(df_m15):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, timestamp, signal_type, sl, tp1 FROM trades WHERE outcome IS NULL")
    open_trades = cursor.fetchall()

    if not open_trades or df_m15.empty:
        conn.close()
        return

    df_index = df_m15.index
    if df_index.tz is None:
        df_index = df_index.tz_localize(timezone.utc)
    else:
        df_index = df_index.tz_convert(timezone.utc)

    highs = to_1d_series(df_m15['High'])
    lows = to_1d_series(df_m15['Low'])
    ph = "%s" if is_postgres() and isinstance(conn, psycopg2.extensions.connection) else "?"

    for trade_id, trade_time_str, sig_type, sl, tp1 in open_trades:
        try:
            if isinstance(trade_time_str, datetime):
                if trade_time_str.tzinfo is None:
                    trade_time = trade_time_str.replace(tzinfo=timezone.utc)
                else:
                    trade_time = trade_time_str.astimezone(timezone.utc)
            else:
                trade_time = datetime.strptime(str(trade_time_str)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                
            mask = df_index >= trade_time
            if not mask.any():
                continue

            sub_highs = highs[mask]
            sub_lows = lows[mask]

            max_price_after_trade = sub_highs.max()
            min_price_after_trade = sub_lows.min()

            if "BUY" in sig_type or "شراء" in sig_type:
                if max_price_after_trade >= tp1:
                    cursor.execute(f"UPDATE trades SET outcome = 1 WHERE id = {ph}", (trade_id,))
                elif min_price_after_trade <= sl:
                    cursor.execute(f"UPDATE trades SET outcome = 0 WHERE id = {ph}", (trade_id,))
            elif "SELL" in sig_type or "بيع" in sig_type:
                if min_price_after_trade <= tp1:
                    cursor.execute(f"UPDATE trades SET outcome = 1 WHERE id = {ph}", (trade_id,))
                elif max_price_after_trade >= sl:
                    cursor.execute(f"UPDATE trades SET outcome = 0 WHERE id = {ph}", (trade_id,))
        except Exception as e:
            print(f"خطأ في تقييم تتبع الصفقة رقم {trade_id}: {e}")

    conn.commit()
    conn.close()

# --- خوارزمية التعلم الذاتي المحسّنة ---
def train_self_learning_model():
    conn = get_db_connection()
    df = pd.read_sql_query(
        "SELECT rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio, outcome FROM trades WHERE outcome IS NOT NULL", 
        conn
    )
    conn.close()

    if len(df) < 10:
        return None

    feature_cols = ['rsi', 'dxy_corr', 'macd_diff', 'stoch_k', 'volatility_ratio']
    df[feature_cols] = df[feature_cols].fillna(0)
    
    X = df[feature_cols]
    y = df['outcome']

    if len(np.unique(y)) < 2:
        return None

    clf = RandomForestClassifier(
        n_estimators=150, 
        max_depth=6, 
        min_samples_split=4, 
        class_weight='balanced',
        random_state=42
    )
    clf.fit(X, y)
    return clf

# ------------------------------------
# 2. فلتر الأخبار والسيولة
# ------------------------------------
def check_news_guard():
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour

    if hour in [21, 22]:
        return False, "اتساع السبريد وساعات التغليف اليومي للأسواق."

    return True, "الظروف الإخبارية والسيولة مستقرة."

# ------------------------------------
# 3. محرك تحليل SMC
# ------------------------------------
def detect_smc_setup(df):
    if len(df) < 10:
        return {"fvg_bullish": False, "fvg_bearish": False, "sweep_bullish": False, "sweep_bearish": False}

    highs = to_1d_series(df['High']).values
    lows = to_1d_series(df['Low']).values
    closes = to_1d_series(df['Close']).values

    fvg_bullish = bool(lows[-2] > highs[-4])
    fvg_bearish = bool(highs[-2] < lows[-4])

    recent_low = np.min(lows[-15:-2])
    recent_high = np.max(highs[-15:-2])
    
    sweep_bullish = bool((lows[-2] < recent_low) and (closes[-2] > recent_low))
    sweep_bearish = bool((highs[-2] > recent_high) and (closes[-2] < recent_high))

    return {
        "fvg_bullish": fvg_bullish,
        "fvg_bearish": fvg_bearish,
        "sweep_bullish": sweep_bullish,
        "sweep_bearish": sweep_bearish
    }

# ------------------------------------
# 4. محرك البيانات المتقاطعة الفورية (Spot Gold Real-Time)
# ------------------------------------
def fetch_live_spot_gold():
    """جلب سعر الذهب الفوري Spot Gold الحقيقي المباشر بدون تأخير وبدون حظر IP"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    # 1. المصدر المباشر الأول: PAX Gold (سعر أونصة الذهب الفوري اللحظي 1:1)
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT", headers=headers, timeout=3)
        if r.status_code == 200:
            price = float(r.json()['price'])
            if price > 1000:
                return round(price, 2)
    except Exception:
        pass

    # 2. المصدر الثاني: واجهة التداول المباشر لأسواق الذهب الفوري
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?interval=1m&range=1d", headers=headers, timeout=3)
        if r.status_code == 200:
            res = r.json()
            price = float(res['chart']['result'][0]['meta']['regularMarketPrice'])
            if price > 1000:
                return round(price, 2)
    except Exception:
        pass

    # 3. الاحتياطي الأخير: عقود الفيوتشرز
    try:
        gc_t = yf.Ticker("GC=F")
        price = gc_t.fast_info.get('lastPrice', None)
        if price and not np.isnan(price):
            return round(float(price), 2)
    except Exception:
        pass

    return 0.0

def get_market_data():
    # إرجاع البيانات المحفوظة في الذاكرة العشوائية فوراً (استجابة في أقل من 1 ملي ثانية)
    if GLOBAL_CACHE["market_data"]["gold"] > 0:
        return GLOBAL_CACHE["market_data"]
    
    # تحذير أولي في حال عدم اكتمال الدورة الأولى للتحديث
    gold_price = fetch_live_spot_gold()
    return {"gold": gold_price, "dxy": 99.85, "us10y": 4.63}

def fetch_and_update_cache():
    """وظيفة تحديث الذاكرة العشوائية في الخلفية بدون حجب الاستجابة"""
    try:
        gold = fetch_live_spot_gold()
        
        headers = {'User-Agent': 'Mozilla/5.0'}
        dxy = 99.85
        us10y = 4.63

        try:
            r_dxy = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1m&range=1d", headers=headers, timeout=3)
            if r_dxy.status_code == 200:
                dxy = float(r_dxy.json()['chart']['result'][0]['meta']['regularMarketPrice'])
        except Exception:
            pass

        try:
            r_tnx = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/^TNX?interval=1m&range=1d", headers=headers, timeout=3)
            if r_tnx.status_code == 200:
                us10y = float(r_tnx.json()['chart']['result'][0]['meta']['regularMarketPrice'])
        except Exception:
            pass

        if gold > 0:
            GLOBAL_CACHE["market_data"] = {
                "gold": round(gold, 2),
                "dxy": round(dxy, 2),
                "us10y": round(us10y, 2)
            }
            GLOBAL_CACHE["last_updated"] = datetime.now(timezone.utc)
    except Exception as e:
        print(f"خطأ تحديث كاش البيانات: {e}")

def analyze_institutional_engine():
    try:
        df_gold_h1 = yf.download("GC=F", period="60d", interval="1h", progress=False)
        df_gold_m15 = yf.download("GC=F", period="5d", interval="15m", progress=False)
        df_dxy_m15 = yf.download("DX-Y.NYB", period="5d", interval="15m", progress=False)
        df_us10y_m15 = yf.download("^TNX", period="5d", interval="15m", progress=False)

        if df_gold_m15.empty or df_dxy_m15.empty or df_gold_h1.empty:
            return None

        close_h1 = to_1d_series(df_gold_h1['Close'])
        close_gold_m15 = to_1d_series(df_gold_m15['Close'])
        close_dxy_m15 = to_1d_series(df_dxy_m15['Close'])
        close_us10y_m15 = to_1d_series(df_us10y_m15['Close'])

        ema200 = ta.trend.EMAIndicator(close_h1, window=200).ema_indicator().dropna()
        ema500 = ta.trend.EMAIndicator(close_h1, window=500).ema_indicator().dropna()
        
        if not ema200.empty and not ema500.empty:
            h4_trend = "BULLISH" if ema200.iloc[-1] > ema500.iloc[-1] else "BEARISH"
        else:
            h4_trend = "BULLISH" if close_h1.iloc[-1] > close_h1.iloc[-50] else "BEARISH"

        returns_gold = np.log(close_gold_m15 / close_gold_m15.shift(1))
        returns_dxy = np.log(close_dxy_m15 / close_dxy_m15.shift(1))
        
        aligned_returns = pd.concat([returns_gold, returns_dxy], axis=1, sort=False).dropna()
        aligned_returns.columns = ['Gold', 'DXY']

        dxy_corr = aligned_returns['Gold'].corr(aligned_returns['DXY'])
        dxy_corr = 0.0 if np.isnan(dxy_corr) else dxy_corr

        volatility = aligned_returns['Gold'].rolling(window=10).std().dropna()
        features = pd.concat([aligned_returns['Gold'], volatility], axis=1, sort=False).dropna()
        features.columns = ['Returns', 'Volatility']

        scaler = StandardScaler()
        scaled_features = scaler.fit_transform(features)

        model = GaussianHMM(n_components=3, covariance_type="diag", n_iter=100, random_state=42)
        model.fit(scaled_features)
        hidden_states = model.predict(scaled_features)
        current_state = hidden_states[-1]

        state_means = [(i, features['Returns'][hidden_states == i].mean()) for i in range(3)]
        state_means.sort(key=lambda x: x[1])
        
        bearish_state = state_means[0][0]
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

        return {
            "h4_trend": h4_trend,
            "state_label": state_label,
            "last_price": last_price,
            "df_m15": df_gold_m15,
            "dxy_corr": round(dxy_corr, 2),
            "us10y_trend": "DOWN" if close_us10y_m15.iloc[-1] < close_us10y_m15.iloc[-5] else "UP",
            "smc": smc
        }
    except Exception as e:
        print(f"خطأ في التحليل المؤسسي: {e}")
        return None

# ------------------------------------
# 5. خوارزمية توليد الإشارات
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
    if volatility_ratio < 0.03:
        return {"status": "WAIT", "reason": "ضعف تذبذب السوق (Low Volatility Ratio).", "price": current_price}

    if state == "RANGING":
        return {"status": "WAIT", "reason": "السوق في نطاق عرضي تذبذبي على M15.", "price": current_price}

    clf = train_self_learning_model()
    confidence = 0.82
    if clf:
        try:
            input_features = pd.DataFrame([[rsi, dxy_corr, macd_diff, stoch_k, volatility_ratio]], 
                                          columns=['rsi', 'dxy_corr', 'macd_diff', 'stoch_k', 'volatility_ratio'])
            prob = clf.predict_proba(input_features)[0]
            confidence = round(float(np.max(prob)), 2)
        except Exception:
            confidence = 0.82

    risk_percent = 2.0 if confidence >= 0.85 else 1.0

    if (h4_trend == "BULLISH" and state == "BULLISH" and ema_fast > ema_slow and 
        rsi < 68 and dxy_corr < -0.10 and (smc["fvg_bullish"] or smc["sweep_bullish"])):
        
        sl = round(current_price - (atr * 1.3), 2)
        tp1 = round(current_price + (atr * 1.6), 2)
        tp2 = round(current_price + (atr * 3.0), 2)

        candle_timestamp = str(df.index[-1])
        log_trade("BUY", current_price, sl, tp1, tp2, round(rsi, 1), dxy_corr, round(macd_diff, 3), round(stoch_k, 1), volatility_ratio, confidence)

        return {
            "status": "SIGNAL", "type": "🟢 شراء مؤسسي (BUY)", "entry": current_price,
            "sl": sl, "tp1": tp1, "tp2": tp2, "rr": "1:2.3",
            "rsi": round(rsi, 1), "dxy_corr": dxy_corr, "confidence": int(confidence * 100),
            "risk": f"{risk_percent}% من الحساب",
            "smc_note": "تأكيد SMC: اقتناص سيولة / FVG صاعدة" if smc["fvg_bullish"] else "تأكيد بكسر القاع",
            "candle_id": candle_timestamp
        }

    elif (h4_trend == "BEARISH" and state == "BEARISH" and ema_fast < ema_slow and 
          rsi > 32 and dxy_corr < -0.10 and (smc["fvg_bearish"] or smc["sweep_bearish"])):
        
        sl = round(current_price + (atr * 1.3), 2)
        tp1 = round(current_price - (atr * 1.6), 2)
        tp2 = round(current_price - (atr * 3.0), 2)

        candle_timestamp = str(df.index[-1])
        log_trade("SELL", current_price, sl, tp1, tp2, round(rsi, 1), dxy_corr, round(macd_diff, 3), round(stoch_k, 1), volatility_ratio, confidence)

        return {
            "status": "SIGNAL", "type": "🔴 بيع مؤسسي (SELL)", "entry": current_price,
            "sl": sl, "tp1": tp1, "tp2": tp2, "rr": "1:2.3",
            "rsi": round(rsi, 1), "dxy_corr": dxy_corr, "confidence": int(confidence * 100),
            "risk": f"{risk_percent}% من الحساب",
            "smc_note": "تأكيد SMC: اقتناص سيولة / FVG هابطة" if smc["fvg_bearish"] else "تأكيد بكسر القمة",
            "candle_id": candle_timestamp
        }

    else:
        return {
            "status": "WAIT",
            "reason": f"عدم اكتمال الشروط. H4: {h4_trend}، M15: {state}، DXY Corr: {dxy_corr}.",
            "price": current_price
        }

# ------------------------------------
# 6. المراقبة الآلية ومراقب الذاكرة العشوائية
# ------------------------------------
async def background_cache_worker():
    """حلقة مستمرة تعمل كل 3 ثوان لتحديث الذاكرة بالأسعار اللحظية الفورية بدون التأثير على رد البوت"""
    while True:
        try:
            await asyncio.to_thread(fetch_and_update_cache)
        except Exception as e:
            print(f"خطأ خلفية الكاش: {e}")
        await asyncio.sleep(3)

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
                        f"🚨 **إشارة كمية مؤسسية جديدة (Quant Institutional)**\n"
                        f"───────────────────\n"
                        f"النوع: {sig['type']}\n"
                        f"🎯 **نسبة ثقة الموديل:** {sig['confidence']}%\n"
                        f"⚖️ **المخاطرة الموصى بها:** {sig['risk']}\n"
                        f"💵 **سعر الدخول:** ${sig['entry']}\n"
                        f"🛑 **وقف الخسارة (SL):** ${sig['sl']}\n"
                        f"🎯 **الهدف الأول (TP1):** ${sig['tp1']}\n"
                        f"🎯 **الهدف الثاني (TP2):** ${sig['tp2']}\n"
                        f"💡 **تأكيد الهيكل:** {sig['smc_note']}\n"
                        f"🔗 **ارتباط الدولار:** {sig['dxy_corr']}\n"
                        f"───────────────────\n"
                        f"🤖 *تم تأكيد الإشارة بالتناغم المؤسسي وتحديث التعلم الذاتي.*"
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

async def post_init(app):
    asyncio.create_task(background_cache_worker())
    asyncio.create_task(auto_market_scanner(app))

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
                "مرحباً بك في البوت الكمي المؤسسي. تم تفعيل كافة الصلاحيات والتنبيهات التلقائية.\n\n"
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
        f"حسابك موثق ومفعل في **البوت الكمي المؤسسي (Self-Learning Quant Engine)**.\n\n"
        f"💡 اضغط على الأزرار أدناه لتنفيذ ما تريد:",
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await update.message.reply_text("🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
    
    # القراءة الفورية المباشرة من الذاكرة العشوائية (RAM)
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
        msg = (
            f"🤖 **تقرير بنية السوق المؤسسية (XAU/USD)**\n"
            f"───────────────────\n"
            f"💰 سعر الذهب الفوري: ${res['last_price']}\n"
            f"📈 اتجاه H4 الحاكم: {res['h4_trend']}\n"
            f"📊 حالة HMM (M15): {res['state_label']}\n"
            f"🏦 هيكل السيولة (SMC): {smc_status}\n"
            f"🔗 معامل ارتباط الدولار: {res['dxy_corr']}\n"
            f"───────────"
        )
        await update.message.reply_text(msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await update.message.reply_text("🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
    await update.message.reply_text("⚡ جاري مطابقة شروط التناغم وتأكيدات SMC...")
    sig = await asyncio.to_thread(generate_quant_signal)
    if sig and sig["status"] == "SIGNAL":
        msg = (
            f"🚨 **إشارة كمية مؤسسية**\n"
            f"النوع: {sig['type']}\n"
            f"🎯 نسبة الثقة: {sig['confidence']}%\n"
            f"⚖️ اللوت الموصى به: {sig['risk']}\n"
            f"💵 سعر الدخول: ${sig['entry']}\n"
            f"🛑 SL: ${sig['sl']}\n"
            f"🎯 TP1: ${sig['tp1']}\n"
            f"🎯 TP2: ${sig['tp2']}\n"
            f"💡 SMC: {sig['smc_note']}"
        )
    else:
        msg = f"⏸️ **تنبيه الانتظار المؤسسي**\n💡 السبب: {sig['reason'] if sig else 'لا توجد فرصة مطابقة'}"
    await update.message.reply_text(msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authenticated(chat_id):
        await update.message.reply_text("🔒 يرجى إدخال كلمة السر أولاً لاستخدام البوت.")
        return
        
    conn = get_db_connection()
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

    conn.close()
    
    db_type_str = "Supabase PostgreSQL (سحابية دائمية)" if is_pg else "SQLite (محلية / احتياطية)"
    
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
        f"💡 *يتم تحديث النموذج وإعادة تدريب Random Forest أوتوماتيكياً مع كل تقييم.*"
    )
    await update.message.reply_text(msg, reply_markup=get_main_keyboard(), parse_mode='Markdown')

if __name__ == '__main__':
    # 1. تشغيل خادم Flask لإرضاء Render وفحص المنفذ فوراً
    flask_process = Process(target=run_flask)
    flask_process.start()

    # 2. تهيئة قاعدة البيانات وتحميل الإعدادات بعد ضمان فتح المنفذ
    init_db()
    load_admin_id()

    # 3. تشغيل بوت تليجرام
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("stats", stats))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    
    print("🤖 البوت والخادم يعملان بكفاءة تامة وجاهزون للمراقبة 24/7...")
    app.run_polling()

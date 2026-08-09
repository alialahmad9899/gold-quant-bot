import asyncio
import html
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# إعدادات تسجيل الأحداث (Logging)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("XAUUSD_QuantBot")

# ---------------------------------------------------------------------------
# التحقق الصارم من التبعيات (Dependencies)
# ---------------------------------------------------------------------------

try:
    import yfinance as yf
except ImportError:
    logger.critical("المكتبة 'yfinance' غير مثبتة. نفذ: pip install yfinance")
    sys.exit(1)

try:
    import ta
except ImportError:
    logger.critical("المكتبة 'ta' غير مثبتة. نفذ: pip install ta")
    sys.exit(1)

try:
    from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
    from telegram.constants import ParseMode
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError:
    logger.critical(
        "المكتبة 'python-telegram-bot' غير مثبتة. "
        "نفذ: pip install \"python-telegram-bot[job-queue]\""
    )
    sys.exit(1)

try:
    import psycopg2
    from psycopg2 import pool
except ImportError:
    psycopg2 = None
    pool = None

try:
    from flask import Flask, jsonify
except ImportError:
    Flask = None


# ---------------------------------------------------------------------------
# الإعدادات والمتغيرات البيئية
# ---------------------------------------------------------------------------

UTC = timezone.utc
M15 = pd.Timedelta(minutes=15)

# الرمز الافتراضي للشموع التاريخية
GOLD_SYMBOL = os.getenv("GOLD_SYMBOL", "GC=F")
DXY_SYMBOL = os.getenv("DXY_SYMBOL", "DX-Y.NYB")
US10Y_SYMBOL = os.getenv("US10Y_SYMBOL", "^TNX")

YF_BAR_TIMESTAMP_MODE = os.getenv("YF_BAR_TIMESTAMP_MODE", "open").lower()
if YF_BAR_TIMESTAMP_MODE not in {"open", "close"}:
    raise ValueError("YF_BAR_TIMESTAMP_MODE must be 'open' or 'close'")

MARKET_FETCH_SECONDS = int(os.getenv("MARKET_FETCH_SECONDS", "60"))
FULL_FETCH_SECONDS = int(os.getenv("FULL_FETCH_SECONDS", "14400"))
CACHE_STALE_SECONDS = int(os.getenv("CACHE_STALE_SECONDS", "300"))
HEALTH_STALE_SECONDS = int(os.getenv("HEALTH_STALE_SECONDS", "600"))
MACRO_STALE_SECONDS = int(os.getenv("MACRO_STALE_SECONDS", "600"))
MAX_SWING_AGE_BARS = int(os.getenv("MAX_SWING_AGE_BARS", "240"))
TRADE_EXPIRY_MINUTES = int(os.getenv("TRADE_EXPIRY_MINUTES", "240"))
TRADE_MONITOR_SECONDS = int(os.getenv("TRADE_MONITOR_SECONDS", "30"))

NEWS_FILTER_ENABLED = os.getenv("NEWS_FILTER_ENABLED", "true").lower() == "true"
NEWS_BLACKOUT_MINUTES = int(os.getenv("NEWS_BLACKOUT_MINUTES", "30"))
ESTIMATED_SPREAD_USD = float(os.getenv("ESTIMATED_SPREAD_USD", "0.25"))

DB_FILE = os.getenv("SQLITE_DB_FILE", "quant_bot.db")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", "10000"))

PG_POOL = None
if DATABASE_URL and psycopg2:
    try:
        PG_POOL = psycopg2.pool.ThreadedConnectionPool(
            1, 10, DATABASE_URL, sslmode="require"
        )
        logger.info("حوض اتصالات PostgreSQL جاهز.")
    except Exception as exc:
        logger.error("فشل إنشاء PostgreSQL pool: %s", exc)
        PG_POOL = None


# ---------------------------------------------------------------------------
# حالات جودة البيانات (Data Quality States)
# ---------------------------------------------------------------------------

class DataQualityState:
    OK = "DATA_OK"
    MACRO_DEGRADED = "DATA_MACRO_DEGRADED"
    STALE = "DATA_STALE"
    GAP = "DATA_GAP"
    INVALID = "DATA_INVALID"
    NEWS_BLACKOUT = "NEWS_BLACKOUT"


def translate_quality_state(state):
    translations = {
        DataQualityState.OK: "ممتازة ✅",
        DataQualityState.MACRO_DEGRADED: "محدودة (غاب المؤشر الكلي) ⚠️",
        DataQualityState.STALE: "بيانات قديمة ⚠️",
        DataQualityState.GAP: "فجوة سعرية ⚠️",
        DataQualityState.INVALID: "غير صالحة ❌",
        DataQualityState.NEWS_BLACKOUT: "حظر الأخبار / عطلة السوق 🚫",
    }
    return translations.get(state, state)


# ---------------------------------------------------------------------------
# دلالات مساعدة (Helper Functions)
# ---------------------------------------------------------------------------

def utc_now():
    return datetime.now(UTC)


def ensure_utc_timestamp(value):
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def clean_df_columns(df):
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(-1)

    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(set(df.columns)):
        return pd.DataFrame()

    idx = pd.to_datetime(df.index, utc=True, errors="coerce")
    valid = ~idx.isna()
    df = df.loc[valid].copy()
    df.index = idx[valid]

    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # معالجة وحذف أي قيم NaN لمنع توقف المحرك
    df = df.ffill().bfill()
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).copy()

    return df


def to_1d_series(value):
    if isinstance(value, pd.DataFrame):
        return value.iloc[:, 0]
    return value


def next_m15_boundary_plus_delay(delay_seconds=10):
    now = pd.Timestamp.now(tz="UTC")
    boundary = now.floor("15min")
    if now >= boundary:
        boundary += pd.Timedelta(minutes=15)
    return boundary.to_pydatetime() + timedelta(seconds=delay_seconds)


def next_boundary_delay_seconds(delay_seconds=10):
    target = next_m15_boundary_plus_delay(delay_seconds)
    return max(0.1, (target - utc_now()).total_seconds())


# ---------------------------------------------------------------------------
# دالة جلب سعر سبوت الذهب المباشر المطابق لمنصات التداول (Forex Spot XAUUSD)
# ---------------------------------------------------------------------------

def fetch_real_forex_spot_gold():
    """جلب سعر سبوت الذهب الفعلي اللحظي المباشر من مزود السيولة لسبوت الذهب."""
    url = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            price = float(data.get("price", 0))
            if price > 0:
                return round(price, 2)
    except Exception as exc:
        logger.debug("فشل جلب سعر السبوت المباشر من المصدر الأول: %s", exc)

    return None


# ---------------------------------------------------------------------------
# جلب مباشر وتجاوز حظر الخوادم (Direct API Fetching with Headers)
# ---------------------------------------------------------------------------

def fetch_yahoo_direct(symbol, range_str="1mo", interval="15m"):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_str}&interval={interval}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            result = data["chart"]["result"][0]
            timestamps = result.get("timestamp", [])
            if not timestamps:
                return pd.DataFrame()
            quote = result["indicators"]["quote"][0]

            df = pd.DataFrame(
                {
                    "Open": quote.get("open", []),
                    "High": quote.get("high", []),
                    "Low": quote.get("low", []),
                    "Close": quote.get("close", []),
                    "Volume": quote.get("volume", [0] * len(timestamps)),
                },
                index=pd.to_datetime(timestamps, unit="s", utc=True),
            )
            return clean_df_columns(df)
    except Exception as exc:
        logger.warning("فشل الجلب المباشر للرمز %s: %s", symbol, exc)
        return pd.DataFrame()


def download_yf(symbol, period, interval):
    try:
        df = yf.download(
            symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        cleaned = clean_df_columns(df)
        if not cleaned.empty:
            return cleaned
    except Exception:
        pass

    time.sleep(0.5)
    return fetch_yahoo_direct(symbol, range_str=period, interval=interval)


def fetch_latest_asset_quote(symbols, fallback_df=None):
    if isinstance(symbols, str):
        symbols = [symbols]

    for sym in symbols:
        for period, interval in [("5d", "1m"), ("5d", "15m"), ("1mo", "1d")]:
            df = download_yf(sym, period, interval)
            if not df.empty:
                close = to_1d_series(df["Close"]).dropna()
                if not close.empty:
                    spot_price = round(float(close.iloc[-1]), 2)
                    ts = ensure_utc_timestamp(df.index[-1]).to_pydatetime()
                    half_spread = ESTIMATED_SPREAD_USD / 2.0
                    bid_price = round(spot_price - half_spread, 2)
                    ask_price = round(spot_price + half_spread, 2)
                    return spot_price, bid_price, ask_price, ts

    if fallback_df is not None and not fallback_df.empty:
        close = to_1d_series(fallback_df["Close"]).dropna()
        if not close.empty:
            spot_price = round(float(close.iloc[-1]), 2)
            ts = ensure_utc_timestamp(fallback_df.index[-1]).to_pydatetime()
            half_spread = ESTIMATED_SPREAD_USD / 2.0
            bid_price = round(spot_price - half_spread, 2)
            ask_price = round(spot_price + half_spread, 2)
            return spot_price, bid_price, ask_price, ts

    return None, None, None, None


# ---------------------------------------------------------------------------
# فلتر الأخبار وحالة إغلاق السوق
# ---------------------------------------------------------------------------

def check_high_impact_news_blackout():
    now = utc_now()

    if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 22):
        return True, "البورصة العالمية مغلقة حالياً (عطلة نهاية الأسبوع). ستستأنف الإشارات والتحليل المباشر فور افتتاح السوق مساء الأحد."

    if not NEWS_FILTER_ENABLED:
        return False, "فلتر الأخبار معطل."

    news_hours_utc = [12, 13, 18, 19]
    if now.hour in news_hours_utc and now.minute < NEWS_BLACKOUT_MINUTES:
        return True, f"فترة أخبار عالية التأثير نشطة ({now.strftime('%H:%M')} UTC)."

    return False, "لا توجد أخبار هامة حالياً."


# ---------------------------------------------------------------------------
# الذاكرة المؤقتة لبيانات السوق (Market Snapshot Cache)
# ---------------------------------------------------------------------------

class MarketSnapshotCache:
    def __init__(self):
        self._lock = threading.RLock()
        self.df_m15 = pd.DataFrame()
        self.macro_data = {
            "gold_spot": None,
            "gold_bid": None,
            "gold_ask": None,
            "gold_spot_time": None,
            "dxy": None,
            "dxy_time": None,
            "us10y": None,
            "us10y_time": None,
        }
        self.last_full_fetch = None
        self.last_fetch_time = None
        self.worker_last_attempt = None
        self.worker_last_success = None
        self.worker_last_error = None
        self.worker_alive = True

    def update_full(self, df_m15, macro_data):
        with self._lock:
            now = utc_now()
            self.df_m15 = df_m15.copy() if df_m15 is not None else pd.DataFrame()
            self.macro_data = dict(macro_data)
            self.last_full_fetch = now
            self.last_fetch_time = now
            self.worker_last_success = now
            self.worker_last_error = None
            self.worker_alive = True

    def update_incremental(self, df_m15_recent, macro_data):
        with self._lock:
            if self.df_m15.empty:
                combined = df_m15_recent.copy() if df_m15_recent is not None else pd.DataFrame()
            else:
                combined = pd.concat([self.df_m15, df_m15_recent])
                combined = combined[~combined.index.duplicated(keep="last")]
                combined = combined.sort_index()

            self.df_m15 = combined.tail(4000)
            self.macro_data = dict(macro_data)
            now = utc_now()
            self.last_fetch_time = now
            self.worker_last_success = now
            self.worker_last_error = None
            self.worker_alive = True

    def mark_attempt(self):
        with self._lock:
            self.worker_last_attempt = utc_now()

    def mark_error(self, error):
        with self._lock:
            self.worker_last_error = str(error)

    def get_snapshot(self):
        with self._lock:
            return (
                self.df_m15.copy(),
                dict(self.macro_data),
                self.last_fetch_time,
                self.last_full_fetch,
                self.worker_alive,
                self.worker_last_attempt,
                self.worker_last_success,
                self.worker_last_error,
            )


SNAPSHOT_CACHE = MarketSnapshotCache()


# ---------------------------------------------------------------------------
# حلقة عمل العامل الخفي (Worker Loop)
# ---------------------------------------------------------------------------

def market_data_worker_loop(stop_event):
    logger.info("بدء خيط جلب بيانات السوق المباشرة.")
    first_run = True
    gold_symbols = [GOLD_SYMBOL, "GC=F", "GLD", "IAU"]
    dxy_symbols = [DXY_SYMBOL, "DX-Y.NYB", "UUP"]
    us10y_symbols = [US10Y_SYMBOL, "^TNX"]

    while not stop_event.is_set():
        SNAPSHOT_CACHE.mark_attempt()

        try:
            _, _, _, last_full, _, _, _, _ = SNAPSHOT_CACHE.get_snapshot()
            now = utc_now()

            need_full = (
                first_run
                or last_full is None
                or (now - last_full).total_seconds() > FULL_FETCH_SECONDS
            )

            df_m15 = pd.DataFrame()
            used_symbol = None

            for sym in gold_symbols:
                for period in ["5d", "7d", "1mo"]:
                    df_m15 = download_yf(sym, period, "15m")
                    if not df_m15.empty and len(df_m15) >= 20:
                        used_symbol = sym
                        break
                if not df_m15.empty:
                    break

            if df_m15.empty:
                for sym in gold_symbols:
                    df_m15 = download_yf(sym, "1mo", "1d")
                    if not df_m15.empty:
                        used_symbol = sym
                        break

            # 1. جلب بيانات السعر التقديرية الاحتياطية من الشمعات
            spot_price, bid, ask, spot_time = fetch_latest_asset_quote(gold_symbols, fallback_df=df_m15)

            # 2. جلب سعر سبوت الذهب الحقيقي المباشر والخاص بمحطات التداول (Forex Spot)
            real_forex_spot = fetch_real_forex_spot_gold()
            if real_forex_spot is not None and real_forex_spot > 0:
                spot_price = real_forex_spot
                half_spread = ESTIMATED_SPREAD_USD / 2.0
                bid = round(spot_price - half_spread, 2)
                ask = round(spot_price + half_spread, 2)
                spot_time = utc_now()

            dxy_val, _, _, dxy_time = fetch_latest_asset_quote(dxy_symbols)
            us10y_val, _, _, us10y_time = fetch_latest_asset_quote(us10y_symbols)

            macro_data = {
                "gold_spot": spot_price,
                "gold_bid": bid,
                "gold_ask": ask,
                "gold_spot_time": spot_time,
                "dxy": dxy_val,
                "dxy_time": dxy_time,
                "us10y": us10y_val,
                "us10y_time": us10y_time,
            }

            if need_full or SNAPSHOT_CACHE.df_m15.empty:
                SNAPSHOT_CACHE.update_full(df_m15, macro_data)
            else:
                SNAPSHOT_CACHE.update_incremental(df_m15, macro_data)

            first_run = False

            if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 22):
                logger.info("السوق مغلق حالياً (عطلة نهاية الأسبوع)، تم تعبئة الذاكرة بأحدث أسعار الإغلاق المتاحة.")

        except Exception as exc:
            logger.error("خطأ في عامل جلب البيانات: %s", exc)
            SNAPSHOT_CACHE.mark_error(exc)

        stop_event.wait(MARKET_FETCH_SECONDS)


# ---------------------------------------------------------------------------
# الشموع المغلقة والتحقق من جودة البيانات
# ---------------------------------------------------------------------------

def get_verified_closed_m15_dataframe(df_m15):
    if df_m15 is None or df_m15.empty:
        return pd.DataFrame()

    df = clean_df_columns(df_m15.copy())
    now = pd.Timestamp.now(tz="UTC")

    if YF_BAR_TIMESTAMP_MODE == "open":
        current_boundary = now.floor("15min")
        return df[df.index < current_boundary].copy()

    return df[df.index + M15 <= now].copy()


def evaluate_data_quality(df_m15, macro_data, fetch_time):
    is_blackout, news_reason = check_high_impact_news_blackout()
    if is_blackout:
        return DataQualityState.NEWS_BLACKOUT, news_reason

    if df_m15 is None or df_m15.empty or len(df_m15) < 30:
        return DataQualityState.INVALID, "مجموعة بيانات M15 تقتضي وجود 30 صف على الأقل."

    if fetch_time is None:
        return DataQualityState.INVALID, "لم يكتمل جلب الذاكرة المؤقتة للسوق بعد."

    cache_age = (utc_now() - ensure_utc_timestamp(fetch_time).to_pydatetime()).total_seconds()
    if cache_age > CACHE_STALE_SECONDS and utc_now().weekday() < 5:
        return DataQualityState.STALE, f"بيانات السوق قديمة ({cache_age:.1f} ثانية)."

    recent = df_m15.tail(80)

    if recent[["Open", "High", "Low", "Close"]].isna().any().any():
        return DataQualityState.GAP, "تم اكتشاف قيم مفقودة (NaN) في الأسعار."

    if recent.index.has_duplicates:
        return DataQualityState.INVALID, "تم اكتشاف طوابع زمنية مكررة."

    highs = recent["High"]
    lows = recent["Low"]
    opens = recent["Open"]
    closes = recent["Close"]

    invalid_ohlc = (
        (highs < lows)
        | (highs < opens)
        | (highs < closes)
        | (lows > opens)
        | (lows > closes)
        | (closes <= 0)
        | (opens <= 0)
    )
    if invalid_ohlc.any():
        return DataQualityState.INVALID, "علاقة غير منطقية في أسعار الشمعة (OHLC)."

    spot_price = macro_data.get("gold_spot")
    if spot_price is None or not np.isfinite(spot_price) or spot_price <= 0:
        return DataQualityState.INVALID, "سعر الذهب المباشر غير متاح حالياً."

    closed = get_verified_closed_m15_dataframe(df_m15)
    if closed.empty:
        return DataQualityState.INVALID, "لا توجد شمعة 15 دقيقة مغلقة ومؤكدة."

    recent_closed = closed.tail(50)
    atr_series = ta.volatility.AverageTrueRange(
        recent_closed["High"],
        recent_closed["Low"],
        recent_closed["Close"],
        window=14,
    ).average_true_range()

    atr_val = atr_series.iloc[-1] if not atr_series.empty else np.nan
    if pd.isna(atr_val) or not np.isfinite(atr_val) or atr_val <= 0:
        return DataQualityState.INVALID, "فشل حساب مؤشر ATR لفحص السلامة."

    last_close = float(recent_closed["Close"].iloc[-1])
    if abs(spot_price - last_close) > atr_val * 15.0 and utc_now().weekday() < 5:
        return (
            DataQualityState.INVALID,
            f"انحراف السعر اللحظي عن إغلاق الشمعة تجاوز النطاق المسموح: المباشر={spot_price:.2f}، الإغلاق={last_close:.2f}.",
        )

    return DataQualityState.OK, "جميع فحوصات السلامة والهيكلية مرت بنجاح."


# ---------------------------------------------------------------------------
# إعادة تجميع شمعات الأربع ساعات (H4)
# ---------------------------------------------------------------------------

def resample_m15_to_h4(df_m15_closed):
    if df_m15_closed is None or df_m15_closed.empty:
        return pd.DataFrame()

    df = clean_df_columns(df_m15_closed.copy())
    result = (
        df.resample(
            "4h",
            origin="start_day",
            closed="right",
            label="right",
        )
        .agg(
            {
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }
        )
        .dropna()
    )
    return result


# ---------------------------------------------------------------------------
# التحليل المؤسسي المتقدم (Advanced Institutional SMC)
# ---------------------------------------------------------------------------

def detect_swings_strictly_past(highs, lows, current_eval_idx, right_bars=3, left_bars=3):
    confirmed_swing_highs = []
    confirmed_swing_lows = []

    upper = current_eval_idx - right_bars
    if upper <= left_bars:
        return [], []

    for i in range(left_bars, upper):
        right_edge = i + right_bars
        if right_edge >= current_eval_idx:
            continue

        high_window = highs[i - left_bars : i + right_bars + 1]
        low_window = lows[i - left_bars : i + right_bars + 1]

        if highs[i] == np.max(high_window):
            confirmed_swing_highs.append(
                {
                    "confirmation_idx": right_edge,
                    "origin_idx": i,
                    "price": float(highs[i]),
                }
            )

        if lows[i] == np.min(low_window):
            confirmed_swing_lows.append(
                {
                    "confirmation_idx": right_edge,
                    "origin_idx": i,
                    "price": float(lows[i]),
                }
            )

    return confirmed_swing_highs, confirmed_swing_lows


def build_structure(swing_highs, swing_lows):
    structure = {
        "high": None,
        "low": None,
        "high_label": None,
        "low_label": None,
    }

    if len(swing_highs) >= 2:
        previous = swing_highs[-2]["price"]
        current = swing_highs[-1]["price"]
        structure["high_label"] = "HH" if current > previous else "LH"

    if len(swing_lows) >= 2:
        previous = swing_lows[-2]["price"]
        current = swing_lows[-1]["price"]
        structure["low_label"] = "HL" if current > previous else "LL"

    return structure


def detect_institutional_smc(df_m15_closed):
    empty = {
        "fvg_bullish": False,
        "fvg_bearish": False,
        "bos_bullish": False,
        "bos_bearish": False,
        "choch_bullish": False,
        "choch_bearish": False,
        "sweep_bullish": False,
        "sweep_bearish": False,
        "ob_bullish": False,
        "ob_bearish": False,
        "is_premium": False,
        "is_discount": False,
        "last_swing_high": None,
        "last_swing_low": None,
        "last_swing_high_origin_idx": None,
        "last_swing_low_origin_idx": None,
        "structure_high": None,
        "structure_low": None,
    }

    if df_m15_closed is None or len(df_m15_closed) < 30:
        return empty

    df = clean_df_columns(df_m15_closed.copy())
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    closes = df["Close"].to_numpy(dtype=float)
    opens = df["Open"].to_numpy(dtype=float)

    eval_idx = len(closes) - 1

    atr = ta.volatility.AverageTrueRange(
        pd.Series(highs),
        pd.Series(lows),
        pd.Series(closes),
        window=14,
    ).average_true_range()
    atr_val = atr.iloc[-1] if not atr.empty else np.nan

    if pd.isna(atr_val) or not np.isfinite(atr_val) or atr_val <= 0:
        return empty

    body_last = abs(closes[-1] - opens[-1])
    is_displacement = body_last > atr_val

    fvg_bullish = bool(
        lows[-1] > highs[-3]
        and is_displacement
        and closes[-1] > opens[-1]
    )
    fvg_bearish = bool(
        highs[-1] < lows[-3]
        and is_displacement
        and closes[-1] < opens[-1]
    )

    swing_highs, swing_lows = detect_swings_strictly_past(
        highs,
        lows,
        eval_idx,
        right_bars=3,
        left_bars=3,
    )

    last_sh = swing_highs[-1] if swing_highs else None
    last_sl = swing_lows[-1] if swing_lows else None

    if last_sh and eval_idx - last_sh["confirmation_idx"] > MAX_SWING_AGE_BARS:
        last_sh = None
    if last_sl and eval_idx - last_sl["confirmation_idx"] > MAX_SWING_AGE_BARS:
        last_sl = None

    structure = build_structure(swing_highs, swing_lows)

    last_swing_high = last_sh["price"] if last_sh else None
    last_swing_low = last_sl["price"] if last_sl else None

    is_premium = False
    is_discount = False
    if last_swing_high is not None and last_swing_low is not None:
        eq_level = (last_swing_high + last_swing_low) / 2.0
        if closes[-1] > eq_level:
            is_premium = True
        elif closes[-1] < eq_level:
            is_discount = True

    bos_bullish = False
    bos_bearish = False

    if last_swing_high is not None:
        bos_bullish = bool(
            closes[-2] <= last_swing_high
            and closes[-1] > last_swing_high
        )

    if last_swing_low is not None:
        bos_bearish = bool(
            closes[-2] >= last_swing_low
            and closes[-1] < last_swing_low
        )

    choch_bullish = bool(
        bos_bullish
        and structure["high_label"] == "LH"
        and structure["low_label"] == "LL"
    )
    choch_bearish = bool(
        bos_bearish
        and structure["high_label"] == "HH"
        and structure["low_label"] == "HL"
    )

    ob_bullish = False
    ob_bearish = False
    if len(closes) >= 5:
        if closes[-2] > opens[-2] and (closes[-2] - opens[-2]) > atr_val:
            ob_low = lows[-3]
            ob_high = highs[-3]
            if lows[-1] <= ob_high and closes[-1] >= ob_low:
                ob_bullish = True

        if closes[-2] < opens[-2] and (opens[-2] - closes[-2]) > atr_val:
            ob_low = lows[-3]
            ob_high = highs[-3]
            if highs[-1] >= ob_low and closes[-1] <= ob_high:
                ob_bearish = True

    recent_low = np.min(lows[-18:-2]) if len(lows) >= 18 else np.min(lows)
    recent_high = np.max(highs[-18:-2]) if len(highs) >= 18 else np.max(highs)

    sweep_bullish = bool(
        lows[-1] < recent_low
        and closes[-1] > recent_low
    )
    sweep_bearish = bool(
        highs[-1] > recent_high
        and closes[-1] < recent_high
    )

    return {
        "fvg_bullish": fvg_bullish,
        "fvg_bearish": fvg_bearish,
        "bos_bullish": bos_bullish,
        "bos_bearish": bos_bearish,
        "choch_bullish": choch_bullish,
        "choch_bearish": choch_bearish,
        "sweep_bullish": sweep_bullish,
        "sweep_bearish": sweep_bearish,
        "ob_bullish": ob_bullish,
        "ob_bearish": ob_bearish,
        "is_premium": is_premium,
        "is_discount": is_discount,
        "last_swing_high": last_swing_high,
        "last_swing_low": last_swing_low,
        "last_swing_high_origin_idx": last_sh["origin_idx"] if last_sh else None,
        "last_swing_low_origin_idx": last_sl["origin_idx"] if last_sl else None,
        "structure_high": structure["high_label"],
        "structure_low": structure["low_label"],
    }


# ---------------------------------------------------------------------------
# إدارة قواعد البيانات (Database)
# ---------------------------------------------------------------------------

def get_db_connection():
    if PG_POOL:
        return PG_POOL.getconn()

    conn = sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def release_db_connection(conn):
    if not conn:
        return

    if PG_POOL:
        PG_POOL.putconn(conn)
    else:
        conn.close()


def db_placeholder():
    return "%s" if PG_POOL else "?"


def init_db():
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        if not PG_POOL:
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA busy_timeout=5000;")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                subscribed INTEGER DEFAULT 1,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        auto_inc = "SERIAL PRIMARY KEY" if PG_POOL else "INTEGER PRIMARY KEY AUTOINCREMENT"

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS signal_logs (
                id {auto_inc},
                candle_id TEXT UNIQUE,
                signal_type TEXT,
                signal_candle_close REAL,
                live_execution_price REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                quality_state TEXT,
                trade_status TEXT DEFAULT 'OPEN',
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tp1_hit_at TIMESTAMP,
                tp2_hit_at TIMESTAMP,
                sl_hit_at TIMESTAMP,
                closed_at TIMESTAMP,
                exit_price REAL,
                slippage REAL,
                realized_r REAL
            )
            """
        )

        conn.commit()
        logger.info("تم تهيئة قاعدة البيانات بنجاح.")
    finally:
        release_db_connection(conn)


def register_user(user_id, username, first_name):
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        if PG_POOL:
            cur.execute(
                """
                INSERT INTO users (user_id, username, first_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name
                """,
                (user_id, username, first_name),
            )
        else:
            cur.execute(
                """
                INSERT INTO users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
                """,
                (user_id, username, first_name),
            )

        conn.commit()
    except Exception as exc:
        logger.error("فشل تسجيل المستخدم: %s", exc)
    finally:
        release_db_connection(conn)


def get_subscribed_users():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE subscribed = 1")
        rows = cur.fetchall()
        return [
            row[0] if isinstance(row, (tuple, list)) else row["user_id"]
            for row in rows
        ]
    except Exception as exc:
        logger.error("فشل جلب المستخدمين المشتركين: %s", exc)
        return []
    finally:
        release_db_connection(conn)


def get_signal_by_candle(candle_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ph = db_placeholder()
        cur.execute(
            f"SELECT * FROM signal_logs WHERE candle_id = {ph}",
            (candle_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            return dict(row)
        if isinstance(row, tuple):
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
        return row
    finally:
        release_db_connection(conn)


def log_signal_to_db(
    candle_id,
    signal_type,
    signal_candle_close,
    live_execution_price,
    sl,
    tp1,
    tp2,
    quality_state,
):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ph = db_placeholder()

        if PG_POOL:
            cur.execute(
                f"""
                INSERT INTO signal_logs (
                    candle_id,
                    signal_type,
                    signal_candle_close,
                    live_execution_price,
                    sl,
                    tp1,
                    tp2,
                    quality_state,
                    trade_status,
                    opened_at
                )
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, 'OPEN', CURRENT_TIMESTAMP)
                ON CONFLICT(candle_id) DO NOTHING
                """,
                (
                    candle_id,
                    signal_type,
                    signal_candle_close,
                    live_execution_price,
                    sl,
                    tp1,
                    tp2,
                    quality_state,
                ),
            )
        else:
            cur.execute(
                f"""
                INSERT OR IGNORE INTO signal_logs (
                    candle_id,
                    signal_type,
                    signal_candle_close,
                    live_execution_price,
                    sl,
                    tp1,
                    tp2,
                    quality_state,
                    trade_status,
                    opened_at
                )
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, 'OPEN', CURRENT_TIMESTAMP)
                """,
                (
                    candle_id,
                    signal_type,
                    signal_candle_close,
                    live_execution_price,
                    sl,
                    tp1,
                    tp2,
                    quality_state,
                ),
            )

        inserted = cur.rowcount > 0
        conn.commit()
        return inserted
    except Exception as exc:
        conn.rollback()
        logger.error("فشل تسجيل الإشارة في قاعدة البيانات: %s", exc)
        return False
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# متابعة دورة حياة الصفقات (Trade Lifecycle)
# ---------------------------------------------------------------------------

VALID_TRANSITIONS = {
    "OPEN": {"TP1_HIT", "TP2_HIT", "SL_HIT", "EXPIRED", "CANCELLED"},
    "TP1_HIT": {"TP2_HIT", "SL_HIT", "EXPIRED", "CANCELLED"},
    "TP2_HIT": set(),
    "SL_HIT": set(),
    "EXPIRED": set(),
    "CANCELLED": set(),
}


def fetch_open_trades():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                id, candle_id, signal_type,
                signal_candle_close, live_execution_price,
                sl, tp1, tp2, trade_status, opened_at
            FROM signal_logs
            WHERE trade_status IN ('OPEN', 'TP1_HIT')
            ORDER BY id ASC
            """
        )
        return cur.fetchall()
    finally:
        release_db_connection(conn)


def _row_value(row, key, index):
    try:
        if isinstance(row, (sqlite3.Row, dict)):
            return row[key]
        return row[index]
    except Exception:
        return None


def update_trade_state(row, new_status, exit_price=None, realized_r=None):
    current_status = _row_value(row, "trade_status", 8)
    if current_status not in VALID_TRANSITIONS:
        return False

    if new_status not in VALID_TRANSITIONS[current_status]:
        return False

    trade_id = _row_value(row, "id", 0)
    now = utc_now().isoformat()
    conn = get_db_connection()

    try:
        cur = conn.cursor()
        ph = db_placeholder()

        if new_status == "TP1_HIT":
            cur.execute(
                f"""
                UPDATE signal_logs
                SET trade_status = {ph},
                    tp1_hit_at = {ph}
                WHERE id = {ph}
                  AND trade_status = {ph}
                """,
                (new_status, now, trade_id, current_status),
            )
        elif new_status == "TP2_HIT":
            cur.execute(
                f"""
                UPDATE signal_logs
                SET trade_status = {ph},
                    tp2_hit_at = {ph},
                    closed_at = {ph},
                    exit_price = {ph},
                    realized_r = {ph}
                WHERE id = {ph}
                  AND trade_status = {ph}
                """,
                (
                    new_status,
                    now,
                    now,
                    exit_price,
                    realized_r,
                    trade_id,
                    current_status,
                ),
            )
        elif new_status == "SL_HIT":
            cur.execute(
                f"""
                UPDATE signal_logs
                SET trade_status = {ph},
                    closed_at = {ph},
                    exit_price = {ph},
                    realized_r = {ph},
                    sl_hit_at = {ph}
                WHERE id = {ph}
                  AND trade_status = {ph}
                """,
                (
                    new_status,
                    now,
                    exit_price,
                    realized_r,
                    now,
                    trade_id,
                    current_status,
                ),
            )
        else:
            cur.execute(
                f"""
                UPDATE signal_logs
                SET trade_status = {ph},
                    closed_at = {ph},
                    exit_price = {ph},
                    realized_r = {ph}
                WHERE id = {ph}
                  AND trade_status = {ph}
                """,
                (
                    new_status,
                    now,
                    exit_price,
                    realized_r,
                    trade_id,
                    current_status,
                ),
            )

        changed = cur.rowcount > 0
        conn.commit()
        return changed
    except Exception as exc:
        conn.rollback()
        logger.error("فشل تحديث حالة الصفقة: %s", exc)
        return False
    finally:
        release_db_connection(conn)


def calculate_trade_r(signal_type, entry, exit_price, sl):
    if None in (entry, exit_price, sl):
        return None

    risk = abs(entry - sl)
    if risk <= 0:
        return None

    if signal_type == "BUY":
        return (exit_price - entry) / risk

    return (entry - exit_price) / risk


def monitor_open_trades():
    _, macro_data, _, _, _, _, _, _ = SNAPSHOT_CACHE.get_snapshot()
    live_price = macro_data.get("gold_spot")

    if live_price is None or not np.isfinite(live_price):
        return

    rows = fetch_open_trades()
    now = utc_now()

    for row in rows:
        signal_type = _row_value(row, "signal_type", 2)
        entry = _row_value(row, "live_execution_price", 4)
        sl = _row_value(row, "sl", 5)
        tp1 = _row_value(row, "tp1", 6)
        tp2 = _row_value(row, "tp2", 7)
        status = _row_value(row, "trade_status", 8)
        opened_at = _row_value(row, "opened_at", 9)

        if opened_at:
            try:
                opened = ensure_utc_timestamp(opened_at).to_pydatetime()
                if now - opened > timedelta(minutes=TRADE_EXPIRY_MINUTES):
                    update_trade_state(row, "EXPIRED")
                    continue
            except Exception:
                pass

        if signal_type == "BUY":
            if live_price >= tp2:
                r = calculate_trade_r(signal_type, entry, tp2, sl)
                update_trade_state(row, "TP2_HIT", tp2, r)
                continue

            if live_price <= sl:
                r = calculate_trade_r(signal_type, entry, sl, sl)
                update_trade_state(row, "SL_HIT", sl, r)
                continue

            if status == "OPEN" and live_price >= tp1:
                r = calculate_trade_r(signal_type, entry, tp1, sl)
                update_trade_state(row, "TP1_HIT", tp1, r)

        elif signal_type == "SELL":
            if live_price <= tp2:
                r = calculate_trade_r(signal_type, entry, tp2, sl)
                update_trade_state(row, "TP2_HIT", tp2, r)
                continue

            if live_price >= sl:
                r = calculate_trade_r(signal_type, entry, sl, sl)
                update_trade_state(row, "SL_HIT", sl, r)
                continue

            if status == "OPEN" and live_price <= tp1:
                r = calculate_trade_r(signal_type, entry, tp1, sl)
                update_trade_state(row, "TP1_HIT", tp1, r)


# ---------------------------------------------------------------------------
# الذاكرة وقفل توليد الإشارات
# ---------------------------------------------------------------------------

EVALUATED_CANDLES_SET = set()
EVALUATED_CANDLES_DEQUE = deque(maxlen=300)
EVALUATED_LOCK = threading.RLock()
LAST_CANDLE_DECISION_CACHE = {}
SIGNAL_GENERATION_LOCK = threading.Lock()


def mark_candle_evaluated(candle_id, decision_data):
    with EVALUATED_LOCK:
        if candle_id in EVALUATED_CANDLES_SET:
            LAST_CANDLE_DECISION_CACHE[candle_id] = decision_data
            return

        if len(EVALUATED_CANDLES_SET) >= EVALUATED_CANDLES_DEQUE.maxlen:
            oldest = EVALUATED_CANDLES_DEQUE.popleft()
            EVALUATED_CANDLES_SET.discard(oldest)
            LAST_CANDLE_DECISION_CACHE.pop(oldest, None)

        EVALUATED_CANDLES_SET.add(candle_id)
        EVALUATED_CANDLES_DEQUE.append(candle_id)
        LAST_CANDLE_DECISION_CACHE[candle_id] = decision_data


def get_cached_candle_decision(candle_id):
    with EVALUATED_LOCK:
        return LAST_CANDLE_DECISION_CACHE.get(candle_id)


# ---------------------------------------------------------------------------
# محرك توليد الإشارات الكمية (Quant Signal Engine)
# ---------------------------------------------------------------------------

def wait_result(reason, quality_state, price=None):
    return {
        "status": "WAIT",
        "quality_state": quality_state,
        "reason": reason,
        "price": round(price, 2) if price is not None else None,
    }


def generate_quant_signal():
    with SIGNAL_GENERATION_LOCK:
        (
            df_m15,
            macro_data,
            fetch_time,
            _,
            _,
            _,
            _,
            _,
        ) = SNAPSHOT_CACHE.get_snapshot()

        quality_state, quality_reason = evaluate_data_quality(
            df_m15,
            macro_data,
            fetch_time,
        )

        if quality_state in {
            DataQualityState.INVALID,
            DataQualityState.STALE,
            DataQualityState.GAP,
            DataQualityState.NEWS_BLACKOUT,
        }:
            return wait_result(
                quality_reason,
                quality_state,
                macro_data.get("gold_spot"),
            )

        df_closed = get_verified_closed_m15_dataframe(df_m15)
        if df_closed.empty:
            return wait_result(
                "لا توجد شمعة 15 دقيقة مغلقة ومؤكدة.",
                DataQualityState.INVALID,
                macro_data.get("gold_spot"),
            )

        closed_time = ensure_utc_timestamp(df_closed.index[-1])
        candle_id = f"XAUUSD_{closed_time.strftime('%Y%m%d_%H%M')}"

        persisted = get_signal_by_candle(candle_id)
        if persisted:
            cached = get_cached_candle_decision(candle_id)
            if cached:
                return cached

            p_quality = (
                persisted.get("quality_state", quality_state)
                if isinstance(persisted, dict)
                else quality_state
            )
            return wait_result(
                f"الشمعة {candle_id} مسجلة سلفاً؛ لن يتم إنتاج إشارة مكررة.",
                p_quality,
                macro_data.get("gold_spot"),
            )

        cached = get_cached_candle_decision(candle_id)
        if cached:
            return cached

        df_h4 = resample_m15_to_h4(df_closed)
        if len(df_h4) < 15:
            return wait_result(
                f"عدد شمعات H4 المغلقة غير كافٍ ({len(df_h4)}/15).",
                quality_state,
                macro_data.get("gold_spot"),
            )

        close_h4 = to_1d_series(df_h4["Close"])
        if len(close_h4) >= 200:
            ema50 = ta.trend.EMAIndicator(close_h4, window=50).ema_indicator().iloc[-1]
            ema200 = ta.trend.EMAIndicator(close_h4, window=200).ema_indicator().iloc[-1]
        elif len(close_h4) >= 50:
            ema50 = ta.trend.EMAIndicator(close_h4, window=50).ema_indicator().iloc[-1]
            ema200 = float(close_h4.mean())
        else:
            ema50 = float(close_h4.tail(10).mean())
            ema200 = float(close_h4.mean())

        if not all(np.isfinite(x) for x in (ema50, ema200)):
            return wait_result(
                "فشل حساب المتوسطات الحسابية EMA لشمعات H4.",
                DataQualityState.INVALID,
                macro_data.get("gold_spot"),
            )

        if ema50 > ema200:
            h4_trend = "BULLISH"
        elif ema50 < ema200:
            h4_trend = "BEARISH"
        else:
            h4_trend = "NEUTRAL"

        smc = detect_institutional_smc(df_closed)

        signal_candle_close = float(df_closed["Close"].iloc[-1])
        live_execution_price = macro_data.get("gold_spot")

        if (
            live_execution_price is None
            or not np.isfinite(live_execution_price)
            or live_execution_price <= 0
        ):
            return wait_result(
                "سعر التنفيذ المباشر غير متاح حالياً.",
                DataQualityState.INVALID,
                signal_candle_close,
            )

        buy_score = 0
        sell_score = 0

        if h4_trend == "BULLISH":
            buy_score += 2
        elif h4_trend == "BEARISH":
            sell_score += 2

        if smc["bos_bullish"] or smc["choch_bullish"]:
            buy_score += 3

        if smc["bos_bearish"] or smc["choch_bearish"]:
            sell_score += 3

        if smc["fvg_bullish"] or smc["sweep_bullish"]:
            buy_score += 2

        if smc["fvg_bearish"] or smc["sweep_bearish"]:
            sell_score += 2

        if smc["ob_bullish"]:
            buy_score += 2
        if smc["ob_bearish"]:
            sell_score += 2

        if smc["is_discount"]:
            buy_score += 1
        elif smc["is_premium"]:
            sell_score += 1

        signal_type = "HOLD"
        if buy_score >= 6 and sell_score < 3 and smc["is_discount"]:
            signal_type = "BUY"
        elif sell_score >= 6 and buy_score < 3 and smc["is_premium"]:
            signal_type = "SELL"

        highs = df_closed["High"]
        lows = df_closed["Low"]
        closes = df_closed["Close"]

        atr = ta.volatility.AverageTrueRange(
            highs, lows, closes, window=14
        ).average_true_range().iloc[-1]

        if pd.isna(atr) or not np.isfinite(atr) or atr <= 0:
            return wait_result(
                "تعذر حساب قيمة مؤشر ATR.",
                DataQualityState.INVALID,
                live_execution_price,
            )

        if signal_type == "BUY":
            exec_price = macro_data.get("gold_ask") or live_execution_price
            sl = round(exec_price - atr * 1.5, 2)
            tp1 = round(exec_price + atr * 1.5, 2)
            tp2 = round(exec_price + atr * 3.0, 2)
        elif signal_type == "SELL":
            exec_price = macro_data.get("gold_bid") or live_execution_price
            sl = round(exec_price + atr * 1.5, 2)
            tp1 = round(exec_price - atr * 1.5, 2)
            tp2 = round(exec_price - atr * 3.0, 2)
        else:
            exec_price = live_execution_price
            sl = tp1 = tp2 = 0.0

        slippage = exec_price - signal_candle_close if signal_type == "BUY" else signal_candle_close - exec_price

        result = {
            "status": "SUCCESS",
            "quality_state": quality_state,
            "candle_id": candle_id,
            "signal": signal_type,
            "h4_trend": h4_trend,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "smc": smc,
            "signal_candle_close": round(signal_candle_close, 2),
            "candle_close_price": round(signal_candle_close, 2),
            "live_execution_price": round(exec_price, 2),
            "price": round(exec_price, 2),
            "slippage": round(slippage, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "dxy": round(macro_data["dxy"], 2) if macro_data["dxy"] is not None else None,
            "us10y": round(macro_data["us10y"], 2) if macro_data["us10y"] is not None else None,
        }

        if signal_type in {"BUY", "SELL"}:
            inserted = log_signal_to_db(
                candle_id,
                signal_type,
                round(signal_candle_close, 2),
                round(exec_price, 2),
                round(sl, 2),
                round(tp1, 2),
                round(tp2, 2),
                quality_state,
            )
            if not inserted:
                return wait_result(
                    f"الشمعة {candle_id} مسجلة سلفاً؛ لن يتم إنتاج إشارة مكررة.",
                    quality_state,
                    exec_price,
                )

        mark_candle_evaluated(candle_id, result)
        return result


# ---------------------------------------------------------------------------
# محرك الاختبار العكسي (Backtest Engine)
# ---------------------------------------------------------------------------

def run_backtest_simulation(initial_balance=10000.0, risk_per_trade=0.01):
    logger.info("جاري تشغيل محاكاة الاختبار العكسي...")
    df_m15 = download_yf("GC=F", "30d", "15m")
    if df_m15.empty:
        df_m15 = download_yf("GLD", "30d", "15m")

    if df_m15.empty or len(df_m15) < 30:
        return "⚠️ البيانات غير كافية للاختبار العكسي حالياً (قد يكون السوق مغلقاً)."

    df_closed = clean_df_columns(df_m15)
    trades = []

    for i in range(30, len(df_closed) - 10):
        sub_df = df_closed.iloc[:i]
        smc = detect_institutional_smc(sub_df)

        df_h4 = resample_m15_to_h4(sub_df)
        if len(df_h4) < 15:
            continue

        close_h4 = to_1d_series(df_h4["Close"])
        ema50 = ta.trend.EMAIndicator(close_h4, window=50).ema_indicator().iloc[-1] if len(close_h4) >= 50 else close_h4.mean()
        ema200 = ta.trend.EMAIndicator(close_h4, window=200).ema_indicator().iloc[-1] if len(close_h4) >= 200 else close_h4.mean()

        h4_trend = "BULLISH" if ema50 > ema200 else "BEARISH" if ema50 < ema200 else "NEUTRAL"

        buy_score = (2 if h4_trend == "BULLISH" else 0) + (3 if smc["bos_bullish"] else 0) + (2 if smc["fvg_bullish"] or smc["ob_bullish"] else 0)
        sell_score = (2 if h4_trend == "BEARISH" else 0) + (3 if smc["bos_bearish"] else 0) + (2 if smc["fvg_bearish"] or smc["ob_bearish"] else 0)

        sig = "BUY" if buy_score >= 6 and smc["is_discount"] else "SELL" if sell_score >= 6 and smc["is_premium"] else "HOLD"

        if sig in {"BUY", "SELL"}:
            entry_price = float(sub_df["Close"].iloc[-1]) + (ESTIMATED_SPREAD_USD if sig == "BUY" else -ESTIMATED_SPREAD_USD)

            atr_val = ta.volatility.AverageTrueRange(sub_df["High"], sub_df["Low"], sub_df["Close"], window=14).average_true_range().iloc[-1]
            if pd.isna(atr_val) or atr_val <= 0:
                continue

            sl = entry_price - (atr_val * 1.5) if sig == "BUY" else entry_price + (atr_val * 1.5)
            tp1 = entry_price + (atr_val * 1.5) if sig == "BUY" else entry_price - (atr_val * 1.5)
            tp2 = entry_price + (atr_val * 3.0) if sig == "BUY" else entry_price - (atr_val * 3.0)

            future_bars = df_closed.iloc[i:i+30]
            hit = "EXPIRED"
            realized_r = 0.0

            for _, bar in future_bars.iterrows():
                high = bar["High"]
                low = bar["Low"]

                if sig == "BUY":
                    if high >= tp2:
                        hit = "TP2_HIT"
                        realized_r = 2.0
                        break
                    elif low <= sl:
                        hit = "SL_HIT"
                        realized_r = -1.0
                        break
                    elif high >= tp1:
                        hit = "TP1_HIT"
                        realized_r = 1.0
                elif sig == "SELL":
                    if low <= tp2:
                        hit = "TP2_HIT"
                        realized_r = 2.0
                        break
                    elif high >= sl:
                        hit = "SL_HIT"
                        realized_r = -1.0
                        break
                    elif low <= tp1:
                        hit = "TP1_HIT"
                        realized_r = 1.0

            trades.append({"signal": sig, "outcome": hit, "r": realized_r})

    if not trades:
        return "📊 اكتمل الاختبار العكسي: لا توجد إشارات استوفت الشروط."

    df_res = pd.DataFrame(trades)
    wins = df_res[df_res["r"] > 0]
    losses = df_res[df_res["r"] < 0]

    win_rate = (len(wins) / len(df_res)) * 100 if len(df_res) > 0 else 0
    total_r = df_res["r"].sum()

    summary = (
        "📊 <b>نتائج الاختبار العكسي (Backtest 30 Days)</b>\n"
        "───────────────────────\n"
        f"🔢 <b>إجمالي الصفقات:</b> <code>{len(df_res)}</code>\n"
        f"✅ <b>الصفقات الرابحة:</b> <code>{len(wins)}</code>\n"
        f"❌ <b>الصفقات الخاسرة:</b> <code>{len(losses)}</code>\n"
        f"🎯 <b>نسبة النجاح (Win Rate):</b> <code>{win_rate:.1f}%</code>\n"
        f"📈 <b>إجمالي الـ R المحقق:</b> <code>+{total_r:.2f}R</code>\n"
        f"💵 <b>الربح التقديري (1% مخاطرة/صفقة):</b> <code>+{total_r * risk_per_trade * 100:.1f}%</code>\n"
    )
    return summary


# ---------------------------------------------------------------------------
# واجهة التليغرام والتقارير المُعرّبة
# ---------------------------------------------------------------------------

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 تحليل لحظي"), KeyboardButton("⚡ توليد إشارة")],
            [KeyboardButton("📈 إحصائيات الأداء"), KeyboardButton("💼 الصفقات المفتوحة")],
            [KeyboardButton("🛡️ حالة النظام"), KeyboardButton("🔔 اشتراك/إلغاء")],
        ],
        resize_keyboard=True,
    )


def get_performance_stats_report():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT trade_status, realized_r, count(*) 
            FROM signal_logs 
            WHERE trade_status != 'OPEN'
            GROUP BY trade_status, realized_r
            """
        )
        rows = cur.fetchall()

        total_trades = 0
        tp1_hits = 0
        tp2_hits = 0
        sl_hits = 0
        total_r = 0.0

        for row in rows:
            status = _row_value(row, "trade_status", 0)
            r_val = _row_value(row, "realized_r", 1) or 0.0
            count = _row_value(row, "count(*)", 2) or 0

            total_trades += count
            if status == "TP1_HIT":
                tp1_hits += count
            elif status == "TP2_HIT":
                tp2_hits += count
            elif status == "SL_HIT":
                sl_hits += count

            total_r += (r_val * count)

        wins = tp1_hits + tp2_hits
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

        text = (
            "📈 <b>تقرير الأداء وإحصائيات البوت</b>\n"
            "───────────────────────\n"
            f"🔢 <b>إجمالي الصفقات المغلقة:</b> <code>{total_trades}</code>\n"
            f"🎯 <b>أهداف TP1 محققة:</b> <code>{tp1_hits}</code>\n"
            f"🚀 <b>أهداف TP2 محققة:</b> <code>{tp2_hits}</code>\n"
            f"🛑 <b>ضرب وقف الخسارة SL:</b> <code>{sl_hits}</code>\n"
            f"📊 <b>نسبة النجاح العامة:</b> <code>{win_rate:.1f}%</code>\n"
            f"🏆 <b>إجمالي العائد المحقق (Total R):</b> <code>+{total_r:.2f}R</code>\n"
        )
        return text
    except Exception as exc:
        logger.error("خطأ في تقرير الأداء: %s", exc)
        return "⚠️ حدث خطأ أثناء استخراج إحصائيات الأداء."
    finally:
        release_db_connection(conn)


def get_active_trades_report():
    rows = fetch_open_trades()
    if not rows:
        return "💼 <b>لا توجد صفقات مفتوحة حالياً.</b>"

    _, macro_data, _, _, _, _, _, _ = SNAPSHOT_CACHE.get_snapshot()
    live_price = macro_data.get("gold_spot") or 0.0

    text = f"💼 <b>الصَّفَقَات المَفْتُوحَة حَالِيّاً ({len(rows)})</b>\n"
    text += "───────────────────────\n"

    for row in rows:
        candle_id = _row_value(row, "candle_id", 1)
        sig_type = _row_value(row, "signal_type", 2)
        entry = _row_value(row, "live_execution_price", 4)
        sl = _row_value(row, "sl", 5)
        tp1 = _row_value(row, "tp1", 6)
        tp2 = _row_value(row, "tp2", 7)
        status = _row_value(row, "trade_status", 8)

        pnl_pips = (live_price - entry) if sig_type == "BUY" else (entry - live_price)
        emoji = "🟢" if pnl_pips >= 0 else "🔴"
        sig_ar = "شراء 🟢" if sig_type == "BUY" else "بيع 🔴"

        text += (
            f"🆔 <b>{candle_id}</b> ({sig_ar})\n"
            f"💵 <b>الدخول:</b> <code>${entry:.2f}</code> | <b>الحالي:</b> <code>${live_price:.2f}</code>\n"
            f"📊 <b>النتيجة اللحظية:</b> {emoji} <code>{pnl_pips:+.2f}$</code>\n"
            f"🎯 <b>TP1:</b> <code>${tp1:.2f}</code> | <b>TP2:</b> <code>${tp2:.2f}</code>\n"
            f"🛑 <b>SL:</b> <code>${sl:.2f}</code> | <b>الحالة:</b> <code>{status}</code>\n"
            "───────────────────────\n"
        )

    return text


# ---------------------------------------------------------------------------
# معالجات أوامر التليغرام (Telegram Handlers)
# ---------------------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    safe_name = html.escape(user.first_name or "المتداول")
    register_user(user.id, user.username, user.first_name)

    text = (
        f"👋 أهلاً بك يا <b>{safe_name}</b> في بوت التداول الكمي المتقدم XAUUSD Quant v3.0.\n\n"
        "<b>الميزات النشطة تلقائياً:</b>\n"
        "• <b>أسعار التداول المباشرة:</b> ربط متطابق مع أسعار سبوت الذهب (XAUUSD) في منصات التداول.\n"
        "• <b>فلتر الأخبار الاقتصادية:</b> حظر التداول أثناء صدور بيانات USD الهامة وعطلات السوق.\n"
        "• <b>حساب الفارق السعري:</b> احتساب دقيق لأسعار العرض والطلب (Bid/Ask).\n"
        "• <b>التحليل المؤسسي (SMC):</b> تتبع مناطق الطلب والعرض والكشوف السعرية.\n"
        "• <b>محرك الاختبار العكسي:</b> تقييم استراتيجيات التداول باستمرار.\n"
        "• <b>لوحة تحكم كاملة:</b> أزرار تفاعلية مريحة دون الحاجة لكتابة أوامر.\n\n"
        "اختر خياراً من القائمة أدناه للبدء:"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res = await asyncio.to_thread(generate_quant_signal)

    if res["status"] == "WAIT":
        await update.message.reply_text(
            f"⚠️ <b>حالة الانتظار:</b>\n{html.escape(res['reason'])}",
            parse_mode=ParseMode.HTML,
        )
        return

    smc = res["smc"]
    h4_trend_raw = res["h4_trend"]
    trend_ar = (
        "صاعد 🟢" if h4_trend_raw == "BULLISH"
        else "هابط 🔴" if h4_trend_raw == "BEARISH"
        else "محايد 🟡"
    )

    quality_ar = translate_quality_state(res["quality_state"])

    live_price_str = f"{res['live_execution_price']:.2f}" if res.get('live_execution_price') else "غير متاح"
    close_price_str = f"{res['signal_candle_close']:.2f}" if res.get('signal_candle_close') else "غير متاح"
    slippage_str = f"{res['slippage']:.2f}" if res.get('slippage') is not None else "0.00"
    dxy_str = f"{res['dxy']:.2f}" if res.get('dxy') is not None else "غير متاح"
    us10y_str = f"{res['us10y']:.2f}%" if res.get('us10y') is not None else "غير متاح"

    text = (
        "📊 <b>التحليل المؤسسي المتقدم (XAUUSD)</b>\n"
        "───────────────────────\n"
        f"💵 <b>سعر المنصة المباشر:</b> <code>${live_price_str}</code>\n"
        f"📉 <b>إغلاق شمعة الإشارة:</b> <code>${close_price_str}</code>\n"
        f"📐 <b>الانزلاق السعري المتوقع:</b> <code>${slippage_str}</code>\n"
        f"📊 <b>مؤشر الدولار (DXY):</b> <code>{dxy_str}</code>\n"
        f"📈 <b>عائد السندات (US10Y):</b> <code>{us10y_str}</code>\n"
        f"🛡️ <b>جودة البيانات:</b> <code>{quality_ar}</code>\n"
        "───────────────────────\n"
        f"🧭 <b>اتجاه فريم H4:</b> {trend_ar}\n"
        f"📦 <b>منطقة طلب (Order Block شرائي):</b> <code>{'نعم ✅' if smc['ob_bullish'] else 'لا ❌'}</code>\n"
        f"📦 <b>منطقة عرض (Order Block بيعي):</b> <code>{'نعم ✅' if smc['ob_bearish'] else 'لا ❌'}</code>\n"
        f"⚖️ <b>منطقة خصم (Discount):</b> <code>{'نعم ✅' if smc['is_discount'] else 'لا ❌'}</code>\n"
        f"⚖️ <b>منطقة علاوة (Premium):</b> <code>{'نعم ✅' if smc['is_premium'] else 'لا ❌'}</code>\n"
        f"🟢 <b>كسر هيكل صاعد (BOS):</b> <code>{'نعم ✅' if smc['bos_bullish'] else 'لا ❌'}</code>\n"
        f"🔴 <b>كسر هيكل هابط (BOS):</b> <code>{'نعم ✅' if smc['bos_bearish'] else 'لا ❌'}</code>\n"
        f"🔄 <b>تغير اتجاه صاعد (CHoCH):</b> <code>{'نعم ✅' if smc['choch_bullish'] else 'لا ❌'}</code>\n"
        f"🔄 <b>تغير اتجاه هابط (CHoCH):</b> <code>{'نعم ✅' if smc['choch_bearish'] else 'لا ❌'}</code>\n"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res = await asyncio.to_thread(generate_quant_signal)

    if res["status"] == "WAIT":
        await update.message.reply_text(
            f"⚠️ <b>حالة الانتظار:</b>\n{html.escape(res['reason'])}",
            parse_mode=ParseMode.HTML,
        )
        return

    sig = res["signal"]
    sig_ar = "شراء 🟢" if sig == "BUY" else "بيع 🔴" if sig == "SELL" else "انتظار (لا توجد إشارة) 🟡"
    quality_ar = translate_quality_state(res["quality_state"])

    live_price_str = f"{res['live_execution_price']:.2f}"
    close_price_str = f"{res['signal_candle_close']:.2f}"
    slippage_str = f"{res['slippage']:.2f}"

    text = (
        f"⚡ <b>الإشارة الكمية: {sig_ar}</b>\n"
        "───────────────────────\n"
        f"🆔 <b>معرف الشمعة:</b> <code>{res['candle_id']}</code>\n"
        f"💵 <b>سعر الدخول (منصة التداول):</b> <code>${live_price_str}</code>\n"
        f"📉 <b>إغلاق الشمعة:</b> <code>${close_price_str}</code>\n"
        f"📐 <b>الانزلاق السعري:</b> <code>${slippage_str}</code>\n"
        f"🛡️ <b>جودة البيانات:</b> <code>{quality_ar}</code>\n"
        "───────────────────────\n"
    )

    if sig in {"BUY", "SELL"}:
        text += (
            f"🛑 <b>وقف الخسارة (SL):</b> <code>${res['sl']:.2f}</code>\n"
            f"🎯 <b>الهدف الأول (TP1):</b> <code>${res['tp1']:.2f}</code>\n"
            f"🎯 <b>الهدف الثاني (TP2):</b> <code>${res['tp2']:.2f}</code>\n"
            f"📊 <b>نقاط التقييم (شراء / بيع):</b> <code>{res['buy_score']} / {res['sell_score']}</code>\n"
            f"🧭 <b>اتجاه H4:</b> <code>{res['h4_trend']}</code>\n"
        )
    else:
        text += "⚠️ <b>القرار:</b> <code>انتظار فرصة أعدل (HOLD)</code>\n"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    (
        df_m15,
        macro,
        fetch_time,
        last_full,
        worker_alive,
        worker_attempt,
        worker_success,
        worker_error,
    ) = SNAPSHOT_CACHE.get_snapshot()

    subscribers = get_subscribed_users()
    now = utc_now()

    cache_age = (
        (now - ensure_utc_timestamp(fetch_time).to_pydatetime()).total_seconds()
        if fetch_time else None
    )

    closed_df = get_verified_closed_m15_dataframe(df_m15)
    if closed_df.empty and not df_m15.empty:
        closed_df = df_m15

    last_candle = (
        closed_df.index[-1].isoformat()
        if not closed_df.empty
        else "غير متاح"
    )

    worker_status_ar = "نشط ويعمل ✅" if worker_alive else "متوقف ❌"
    error_ar = worker_error if worker_error else "لا يوجد أخطاء"

    gold_spot = f"{macro.get('gold_spot'):.2f}" if macro.get("gold_spot") is not None else "غير متاح"
    gold_bid = f"{macro.get('gold_bid'):.2f}" if macro.get("gold_bid") is not None else "N/A"
    gold_ask = f"{macro.get('gold_ask'):.2f}" if macro.get("gold_ask") is not None else "N/A"
    dxy_val = f"{macro.get('dxy'):.2f}" if macro.get("dxy") is not None else "غير متاح"
    us10y_str = f"{macro.get('us10y'):.2f}%" if macro.get("us10y") is not None else "غير متاح"

    text = (
        "🛡️ <b>حالة النظام والخدمة v3.0</b>\n"
        "───────────────────────\n"
        f"⚙️ <b>العامل الخفي (Worker):</b> <code>{worker_status_ar}</code>\n"
        f"👥 <b>المشتركون في الإشارات:</b> <code>{len(subscribers)}</code>\n"
        f"🧠 <b>الشمعات المقيمة بالذاكرة:</b> <code>{len(df_m15)}</code>\n"
        f"💵 <b>سعر الذهب المباشر (منصة التداول):</b> <code>${gold_spot}</code>\n"
        f"💵 <b>أسعار الطلب/العرض:</b> <code>${gold_bid} / ${gold_ask}</code>\n"
        f"📊 <b>مؤشر الدولار (DXY):</b> <code>{dxy_val}</code>\n"
        f"📈 <b>عائد السندات (US10Y):</b> <code>{us10y_str}</code>\n"
        f"⏱️ <b>عمر الذاكرة المؤقتة:</b> <code>{f'{cache_age:.1f} ثانية' if cache_age is not None else 'غير متاح'}</code>\n"
        f"🕯️ <b>آخر شمعة M15 مغلقة:</b> <code>{last_candle}</code>\n"
        f"⚠️ <b>آخر خطأ في العامل:</b> <code>{error_ar}</code>\n"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def toggle_sub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username, user.first_name)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        ph = db_placeholder()
        cur.execute(
            f"SELECT subscribed FROM users WHERE user_id = {ph}",
            (user.id,),
        )
        row = cur.fetchone()

        current = row[0] if row else 1
        new_value = 0 if current == 1 else 1

        cur.execute(
            f"UPDATE users SET subscribed = {ph} WHERE user_id = {ph}",
            (new_value, user.id),
        )
        conn.commit()
    finally:
        release_db_connection(conn)

    msg = (
        "✅ تم تفعيل اشتراكك بنجاح. ستصلك الإشارات التلقائية فور توليدها."
        if new_value == 1
        else "🛑 تم إيقاف الاشتراك بنجاح. لن تصلك الإشارات التلقائية بعد الآن."
    )
    await update.message.reply_text(msg)


async def handle_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "📊 تحليل لحظي":
        await analyze_cmd(update, context)
    elif text == "⚡ توليد إشارة":
        await signal_cmd(update, context)
    elif text == "📈 إحصائيات الأداء":
        report = get_performance_stats_report()
        await update.message.reply_text(report, parse_mode=ParseMode.HTML)
    elif text == "💼 الصفقات المفتوحة":
        report = get_active_trades_report()
        await update.message.reply_text(report, parse_mode=ParseMode.HTML)
    elif text == "🛡️ حالة النظام":
        await status_cmd(update, context)
    elif text == "🔔 اشتراك/إلغاء":
        await toggle_sub_cmd(update, context)


# ---------------------------------------------------------------------------
# المجدول الزمني للوظائف الخفية (Background Boundary Schedulers)
# ---------------------------------------------------------------------------

async def auto_signal_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        res = await asyncio.to_thread(generate_quant_signal)

        if res["status"] == "SUCCESS" and res["signal"] in {"BUY", "SELL"}:
            users = get_subscribed_users()
            sig = res["signal"]
            emoji = "🟢" if sig == "BUY" else "🔴"
            sig_ar = "شراء" if sig == "BUY" else "بيع"

            message = (
                f"🚨 {emoji} <b>إشارة تلقائية جديدة: {sig_ar}</b>\n"
                "───────────────────────\n"
                f"🆔 <b>معرف الشمعة:</b> <code>{res['candle_id']}</code>\n"
                f"💵 <b>سعر الدخول (منصة التداول):</b> <code>${res['live_execution_price']:.2f}</code>\n"
                f"📉 <b>إغلاق الشمعة:</b> <code>${res['signal_candle_close']:.2f}</code>\n"
                f"📐 <b>الانزلاق السعري:</b> <code>${res['slippage']:.2f}</code>\n"
                f"🛑 <b>وقف الخسارة (SL):</b> <code>${res['sl']:.2f}</code>\n"
                f"🎯 <b>الهدف الأول (TP1):</b> <code>${res['tp1']:.2f}</code>\n"
                f"🎯 <b>الهدف الثاني (TP2):</b> <code>${res['tp2']:.2f}</code>\n"
                f"🧭 <b>اتجاه H4:</b> <code>{res['h4_trend']}</code>\n"
            )

            for uid in users:
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=message,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as exc:
                    logger.warning("فشل البث للمستخدم %s: %s", uid, exc)

    except Exception as exc:
        logger.exception("فشلت وظيفة البث التلقائي للإشارات: %s", exc)


async def trade_lifecycle_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.to_thread(monitor_open_trades)
    except Exception as exc:
        logger.exception("فشلت وظيفة متابعة دورة حياة الصفقات: %s", exc)


async def schedule_boundary_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await auto_signal_job(context)
    finally:
        delay = next_boundary_delay_seconds(10)
        if context.job_queue:
            context.job_queue.run_once(schedule_boundary_job, when=delay)


# ---------------------------------------------------------------------------
# خادم الفحص التشخيصي (Flask Health Server)
# ---------------------------------------------------------------------------

def run_flask_server():
    if Flask is None:
        logger.warning("Flask غير متاح؛ تم تعطيل نقطة فحص الصحة.")
        return

    app = Flask(__name__)

    @app.route("/")
    @app.route("/health")
    def health():
        (
            df_m15,
            macro,
            fetch_time,
            last_full,
            worker_alive,
            worker_attempt,
            worker_success,
            worker_error,
        ) = SNAPSHOT_CACHE.get_snapshot()

        now = utc_now()
        reasons = []
        healthy = True

        if not worker_alive:
            healthy = False
            reasons.append("market_worker_not_healthy")

        cache_age = None
        if fetch_time:
            cache_age = (now - ensure_utc_timestamp(fetch_time).to_pydatetime()).total_seconds()

        if (cache_age is None or cache_age > HEALTH_STALE_SECONDS) and utc_now().weekday() < 5:
            healthy = False
            reasons.append("market_cache_stale")

        db_ok = True
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        except Exception as exc:
            db_ok = False
            healthy = False
            reasons.append(f"database_error:{exc}")
        finally:
            release_db_connection(conn)

        closed = get_verified_closed_m15_dataframe(df_m15)
        last_candle = closed.index[-1].isoformat() if not closed.empty else None

        response = {
            "status": "healthy" if healthy else "unhealthy",
            "worker_alive": worker_alive,
            "database_ok": db_ok,
            "cache_age_seconds": round(cache_age, 2) if cache_age is not None else None,
            "last_closed_m15": last_candle,
            "last_full_fetch": str(last_full) if last_full else None,
            "worker_last_attempt": str(worker_attempt) if worker_attempt else None,
            "worker_last_success": str(worker_success) if worker_success else None,
            "worker_last_error": worker_error,
            "gold_spot_time": str(macro.get("gold_spot_time")),
            "dxy_time": str(macro.get("dxy_time")),
            "us10y_time": str(macro.get("us10y_time")),
            "reasons": reasons,
        }

        return jsonify(response), 200 if healthy else 503

    app.run(host="0.0.0.0", port=PORT, threaded=True)


# ---------------------------------------------------------------------------
# نقطة التشغيل الرئيسية (Main Entrypoint)
# ---------------------------------------------------------------------------

def main():
    init_db()

    stop_event = threading.Event()

    # 1. تشغيل سيرفر Flask فوراً للربط مع المنفذ (Port Binding) بدون أي تأخير
    if Flask is not None:
        flask_thread = threading.Thread(
            target=run_flask_server,
            daemon=True,
            name="health-server",
        )
        flask_thread.start()

    # 2. تشغيل خيط جلب بيانات السوق المباشرة
    market_thread = threading.Thread(
        target=market_data_worker_loop,
        args=(stop_event,),
        daemon=True,
        name="market-data-worker",
    )
    market_thread.start()

    # 3. تشغيل محاكاة الاختبار العكسي في خيط خلفي غير معطل لعملية البداية
    def async_backtest():
        try:
            bt_summary = run_backtest_simulation()
            clean_text = bt_summary.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
            logger.info("\n" + clean_text)
        except Exception as exc:
            logger.warning("تم تخطي الاختبار العكسي الأولي: %s", exc)

    bt_thread = threading.Thread(
        target=async_backtest,
        daemon=True,
        name="backtest-worker",
    )
    bt_thread.start()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.critical("رمز TELEGRAM_BOT_TOKEN مفقود.")
        sys.exit(1)

    application = ApplicationBuilder().token(token).build()

    if application.job_queue:
        application.job_queue.run_repeating(
            trade_lifecycle_job,
            interval=TRADE_MONITOR_SECONDS,
            first=5,
        )

        first_delay = next_boundary_delay_seconds(10)
        application.job_queue.run_once(
            schedule_boundary_job,
            when=first_delay,
        )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_buttons)
    )

    logger.info("تم تشغيل محرك XAUUSD Quant Bot v3.0 بنجاح.")

    try:
        application.run_polling(drop_pending_updates=True)
    finally:
        logger.info("جاري إيقاف الخدمة...")
        stop_event.set()


if __name__ == "__main__":
    main()

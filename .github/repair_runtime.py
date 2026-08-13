from pathlib import Path
import subprocess

BOT = Path("bot.py")
TEST = Path("tests/test_runtime_regressions.py")
WORKFLOW = Path(".github/workflows/runtime-regression-test.yml")
SCRIPT = Path(".github/repair_runtime.py")

s = BOT.read_text(encoding="utf-8")

old = "YAHOO_LIVE_COOLDOWN_SECONDS = int(os.getenv('YAHOO_LIVE_COOLDOWN_SECONDS', '60'))\nYAHOO_HISTORICAL_COOLDOWN_SECONDS = int(os.getenv('YAHOO_HISTORICAL_COOLDOWN_SECONDS', '300'))\n"
new = "YAHOO_LIVE_COOLDOWN_SECONDS = max(60, int(os.getenv('YAHOO_LIVE_COOLDOWN_SECONDS', '60')))\nYAHOO_HISTORICAL_COOLDOWN_SECONDS = max(300, int(os.getenv('YAHOO_HISTORICAL_COOLDOWN_SECONDS', '300')))\n"
assert old in s, "Yahoo cooldown constants anchor not found"
s = s.replace(old, new, 1)

old = "YAHOO_HTTP_DIAGNOSTIC = {'status': None, 'host': None, 'transport': None, 'error_type': None, 'message': None}\n"
new = old + """YAHOO_AUX_LIVE_CACHE = {}\nYAHOO_AUX_LIVE_CACHE_TTL = 60.0\nYAHOO_AUX_LIVE_CACHE_LOCK = threading.Lock()\n\n"""
assert old in s, "Yahoo diagnostic anchor not found"
s = s.replace(old, new, 1)

old = """def fetch_yahoo_direct(symbol, range_str='10d', interval_str='15m'):\n"""
new = """def fetch_yahoo_aux_live(symbol, ttl=60.0):\n    key = str(symbol)\n    now = time.monotonic()\n    with YAHOO_AUX_LIVE_CACHE_LOCK:\n        cached = YAHOO_AUX_LIVE_CACHE.get(key)\n        if cached and now - cached.get('last_checked', 0.0) < float(ttl):\n            return cached.get('value')\n        data = http_get_yahoo(\n            f'https://query1.finance.yahoo.com/v8/finance/chart/{key}?interval=1m&range=1d',\n            timeout=4,\n        )\n        value = None\n        if data and (data.get('chart') or {}).get('result'):\n            try:\n                meta = data['chart']['result'][0].get('meta') or {}\n                value = _finite_positive(meta.get('regularMarketPrice'))\n            except Exception:\n                value = None\n        previous = cached.get('value') if cached else None\n        YAHOO_AUX_LIVE_CACHE[key] = {\n            'value': value if value is not None else previous,\n            'last_checked': now,\n        }\n        return YAHOO_AUX_LIVE_CACHE[key]['value']\n\ndef fetch_yahoo_direct(symbol, range_str='10d', interval_str='15m'):\n"""
assert old in s, "Yahoo direct fetch anchor not found"
s = s.replace(old, new, 1)

old = """            cooldown = YAHOO_HISTORICAL_COOLDOWN_SECONDS\n            if status in (403, 429):\n                cooldown = 900\n            elif status is not None and status >= 500:\n                cooldown = 120\n"""
new = """            cooldown = YAHOO_HISTORICAL_COOLDOWN_SECONDS\n            if status == 404:\n                cooldown = 1800\n            elif status in (403, 429):\n                cooldown = 900\n            elif status is not None and status >= 500:\n                cooldown = 120\n"""
assert old in s, "Historical cooldown branch not found"
s = s.replace(old, new, 1)

old = """            status = diag.get('status')\n            cooldown = 900 if status in (403, 429) else (120 if status is not None and status >= 500 else 60)\n"""
new = """            status = diag.get('status')\n            cooldown = 1800 if status == 404 else (900 if status in (403, 429) else (120 if status is not None and status >= 500 else YAHOO_LIVE_COOLDOWN_SECONDS))\n"""
assert old in s, "Live cooldown branch not found"
s = s.replace(old, new, 1)

old = """        dxy = None\n        data_dxy = http_get_yahoo('https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1m&range=1d', timeout=4)\n        if data_dxy and (data_dxy.get('chart') or {}).get('result'):\n            try:\n                meta = data_dxy['chart']['result'][0]['meta']\n                value = _finite_positive(meta.get('regularMarketPrice'))\n                dxy = round(value, 4) if value is not None else None\n            except Exception:\n                dxy = None\n\n        us10y = None\n        data_tnx = http_get_yahoo('https://query1.finance.yahoo.com/v8/finance/chart/^TNX?interval=1m&range=1d', timeout=4)\n        if data_tnx and (data_tnx.get('chart') or {}).get('result'):\n            try:\n                meta = data_tnx['chart']['result'][0]['meta']\n                value = _finite_positive(meta.get('regularMarketPrice'))\n                us10y = round(value, 4) if value is not None else None\n            except Exception:\n                us10y = None\n"""
new = """        dxy_value = fetch_yahoo_aux_live('DX-Y.NYB', ttl=60.0)\n        dxy = round(float(dxy_value), 4) if dxy_value is not None else None\n\n        us10y_value = fetch_yahoo_aux_live('^TNX', ttl=60.0)\n        us10y = round(float(us10y_value), 4) if us10y_value is not None else None\n"""
assert old in s, "Auxiliary Yahoo fetch block not found"
s = s.replace(old, new, 1)

anchor = """if __name__ == '__main__':\n"""
lock_code = r'''TELEGRAM_POLL_LOCK_KEY = 492817361
TELEGRAM_POLL_LOCK_CONN = None


def acquire_telegram_poll_lock(wait_seconds=90):
    global TELEGRAM_POLL_LOCK_CONN
    if not DATABASE_URL or not str(DATABASE_URL).lower().startswith(('postgresql://', 'postgres://')):
        logger.warning('[TELEGRAM] PostgreSQL advisory lock unavailable; relying on Render single-instance configuration.')
        return True
    deadline = time.monotonic() + max(5, int(wait_seconds))
    while time.monotonic() < deadline:
        conn = None
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute('SELECT pg_try_advisory_lock(%s)', (TELEGRAM_POLL_LOCK_KEY,))
                locked = bool(cur.fetchone()[0])
            if locked:
                TELEGRAM_POLL_LOCK_CONN = conn
                logger.info('[TELEGRAM] Acquired singleton polling lock.')
                return True
        except Exception as exc:
            logger.warning('[TELEGRAM] Polling lock attempt failed: %s', str(exc)[:250])
        finally:
            if conn is not None and TELEGRAM_POLL_LOCK_CONN is not conn:
                try:
                    conn.close()
                except Exception:
                    pass
        time.sleep(5)
    return False


def release_telegram_poll_lock():
    global TELEGRAM_POLL_LOCK_CONN
    conn = TELEGRAM_POLL_LOCK_CONN
    TELEGRAM_POLL_LOCK_CONN = None
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_unlock(%s)', (TELEGRAM_POLL_LOCK_KEY,))
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


'''
assert anchor in s, "main guard anchor not found"
s = s.replace(anchor, lock_code + anchor, 1)

old = """    print(\"🤖 البوت الهجين (Quant + Pure Dynamic Gemini Discovery + Walk-Forward ML) يعمل بكفاءة تامة...\")\n    app.run_polling(drop_pending_updates=True)\n"""
new = """    print(\"🤖 البوت الهجين (Quant + Pure Dynamic Gemini Discovery + Walk-Forward ML) يعمل بكفاءة تامة...\")\n    if not acquire_telegram_poll_lock():\n        raise RuntimeError(\"لم يتم الحصول على قفل Telegram singleton؛ يوجد مثيل آخر يعمل أو قاعدة البيانات غير متاحة.\")\n    try:\n        app.run_polling(drop_pending_updates=True)\n    finally:\n        release_telegram_poll_lock()\n"""
assert old in s, "polling startup anchor not found"
s = s.replace(old, new, 1)

BOT.write_text(s, encoding='utf-8')
subprocess.run(['pytest', '-q', 'tests/test_runtime_regressions.py'], check=True)

for path in (TEST, WORKFLOW, SCRIPT):
    path.unlink(missing_ok=True)

subprocess.run(['git', 'config', 'user.name', 'github-actions[bot]'], check=True)
subprocess.run(['git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com'], check=True)
subprocess.run(['git', 'add', 'bot.py'], check=True)
subprocess.run(['git', 'commit', '-m', 'fix: harden Yahoo feed and Telegram singleton polling'], check=True)
subprocess.run(['git', 'push', 'origin', 'HEAD:main'], check=True)

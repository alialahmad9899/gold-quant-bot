from pathlib import Path
import ast
import math
import subprocess
import threading
import time
from datetime import datetime, timezone

BOT = Path('bot.py')
WORKFLOW = Path('.github/workflows/restore_yahoo_spot_feed.yml')
SCRIPT1 = Path('.github/restore_yahoo_spot_feed.py')
SCRIPT2 = Path('.github/restore_yahoo_spot_feed_v2.py')

LIVE_BLOCK = r'''def _build_missing_yahoo_feed(error_type='missing', error_message='No live XAUUSD Spot data available.'):
    now = datetime.now(timezone.utc)
    return {'symbol':'XAUUSD','provider':'Yahoo Finance','status':'MISSING','bid':None,'ask':None,'mid':None,'spot':None,'source_timestamp':None,'timestamp':None,'fetched_timestamp':now.isoformat(),'source_fetched_timestamp':None,'age_seconds':None,'error_type':str(error_type),'error_message':str(error_message or '')[:300],'spread_available':False}


def _yahoo_live_error_message(response):
    try:
        payload = response.json(); chart = payload.get('chart') or {}; err = chart.get('error')
        if isinstance(err, dict): return str(err.get('description') or err.get('code') or '')[:300]
    except Exception: pass
    try: return str(response.text or '')[:300]
    except Exception: return ''


def _log_yahoo_live_failure(error_type, http_status=None, message=''):
    logger.warning('[XAUUSD_FEED] Yahoo Spot failure type=%s http_status=%s message=%s', error_type, http_status, str(message or '')[:300])


def _set_yahoo_live_cooldown(error_type, error_message):
    with YAHOO_STATE_LOCK:
        YAHOO_FEED_STATE['blocked_until'] = time.monotonic() + max(1, YAHOO_COOLDOWN_SECONDS)
        YAHOO_FEED_STATE['error_type'] = str(error_type); YAHOO_FEED_STATE['error_message'] = str(error_message or '')[:300]
    logger.warning('[XAUUSD_FEED] Yahoo Spot cooldown type=%s seconds=%s message=%s', error_type, YAHOO_COOLDOWN_SECONDS, str(error_message or '')[:300])


def _yahoo_live_cooldown_active():
    with YAHOO_STATE_LOCK: return time.monotonic() < float(YAHOO_FEED_STATE.get('blocked_until', 0.0) or 0.0)


def _build_yahoo_live_feed(payload, *, fetched_at=None):
    now = fetched_at or datetime.now(timezone.utc)
    if not isinstance(payload, dict): return _build_missing_yahoo_feed('malformed_payload','Yahoo returned a non-object payload.')
    result = ((payload.get('chart') or {}).get('result') or [])
    if not result or not isinstance(result[0], dict): return _build_missing_yahoo_feed('missing_result','Yahoo response does not contain chart.result.')
    meta = result[0].get('meta') or {}
    try: price = float(meta.get('regularMarketPrice'))
    except (TypeError,ValueError): price = None
    if price is None or not math.isfinite(price) or price <= 1000: return _build_missing_yahoo_feed('missing_price','Yahoo XAUUSD=X regularMarketPrice is missing or invalid.')
    try: source_ts = datetime.fromtimestamp(float(meta.get('regularMarketTime')), tz=timezone.utc)
    except (TypeError,ValueError,OverflowError): source_ts = None
    if source_ts is None: return _build_missing_yahoo_feed('missing_timestamp','Yahoo XAUUSD=X regularMarketTime is missing or invalid.')
    age = max(0.0,(now-source_ts).total_seconds())
    def finite_side(v):
        try:
            x=float(v); return x if math.isfinite(x) and x>1000 else None
        except (TypeError,ValueError): return None
    bid, ask = finite_side(meta.get('bid')), finite_side(meta.get('ask'))
    spread_available = bid is not None and ask is not None
    if not spread_available: bid = ask = price
    mid = (bid + ask) / 2.0
    return {'symbol':'XAUUSD','provider':'Yahoo Finance','status':'STALE' if age>PRICE_FEED_STALE_SECONDS else 'ACTIVE','bid':round(bid,6),'ask':round(ask,6),'mid':round(mid,6),'spot':round(price,6),'source_timestamp':source_ts.isoformat(),'timestamp':source_ts.isoformat(),'fetched_timestamp':now.isoformat(),'source_fetched_timestamp':None,'age_seconds':round(age,3),'error_type':None,'error_message':None,'spread_available':spread_available}


def fetch_canonical_xauusd_feed():
    """Canonical live XAUUSD Spot from Yahoo XAUUSD=X only; no futures/crypto/scraping fallback."""
    now_m = time.monotonic()
    with PRICE_FEED_LOCK:
        cached = CANONICAL_XAUUSD_FEED_CACHE.get('feed'); last = float(CANONICAL_XAUUSD_FEED_CACHE.get('last_request_monotonic',0.0) or 0.0)
        if now_m-last < PRICE_FEED_MIN_REQUEST_INTERVAL: return _refresh_cached_feed_status(cached)
        if _yahoo_live_cooldown_active(): return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed(YAHOO_FEED_STATE.get('error_type') or 'cooldown', YAHOO_FEED_STATE.get('error_message') or 'Yahoo Spot cooldown is active.')
        CANONICAL_XAUUSD_FEED_CACHE['last_request_monotonic'] = now_m
        headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36','Accept':'application/json','Referer':'https://finance.yahoo.com/'}
        try:
            response=requests.get('https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X',params={'interval':'1m','range':'1d'},headers=headers,timeout=5)
        except requests.Timeout as exc:
            msg=str(exc)[:300]; _set_yahoo_live_cooldown('timeout',msg); _log_yahoo_live_failure('timeout',None,msg); return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('timeout',msg)
        except requests.RequestException as exc:
            msg=str(exc)[:300]; _set_yahoo_live_cooldown('network',msg); _log_yahoo_live_failure('network',None,msg); return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('network',msg)
        except Exception as exc:
            msg=str(exc)[:300]; _set_yahoo_live_cooldown('network',msg); _log_yahoo_live_failure('network',None,msg); return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('network',msg)
        code=int(getattr(response,'status_code',0) or 0)
        if code==429:
            msg=_yahoo_live_error_message(response) or 'Yahoo HTTP 429 rate limit.'; _set_yahoo_live_cooldown('rate_limited',msg); _log_yahoo_live_failure('rate_limited',code,msg); return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('rate_limited',msg)
        if 500<=code<=599:
            msg=_yahoo_live_error_message(response) or f'Yahoo HTTP {code}.'; _set_yahoo_live_cooldown('server_error',msg); _log_yahoo_live_failure('server_error',code,msg); return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('server_error',msg)
        if code!=200:
            msg=_yahoo_live_error_message(response) or f'Yahoo HTTP {code}.'; _log_yahoo_live_failure('http_error',code,msg); return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('http_error',msg)
        try: payload=response.json()
        except Exception as exc:
            msg=str(exc)[:300]; _set_yahoo_live_cooldown('malformed_json',msg); _log_yahoo_live_failure('malformed_json',code,msg); return _refresh_cached_feed_status(cached) if cached else _build_missing_yahoo_feed('malformed_json',msg)
        feed=_build_yahoo_live_feed(payload,fetched_at=datetime.now(timezone.utc))
        if feed.get('status')=='MISSING':
            _set_yahoo_live_cooldown(feed.get('error_type') or 'invalid_data',feed.get('error_message') or 'Yahoo live data is invalid.'); CANONICAL_XAUUSD_FEED_CACHE['feed']=feed; _log_yahoo_live_failure(feed.get('error_type'),code,feed.get('error_message')); return feed
        with YAHOO_STATE_LOCK:
            YAHOO_FEED_STATE['blocked_until']=0.0; YAHOO_FEED_STATE['error_type']=None; YAHOO_FEED_STATE['error_message']=None
        CANONICAL_XAUUSD_FEED_CACHE['feed']=feed; return feed
'''

def replace_top_level_function(src,name,replacement):
    tree=ast.parse(src); node=next(n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name); lines=src.splitlines(True)
    return ''.join(lines[:node.lineno-1]+[replacement.strip()+'\n\n']+lines[node.end_lineno:])

def patch():
    text=BOT.read_text(encoding='utf-8')
    text=''.join(line for line in text.splitlines(True) if not any(k in line for k in ('ARGENT_API_KEY =','METALS_DEV_API_KEY =')))
    text=text.replace('ArgentAPI','Yahoo Finance')
    text=text.replace("df_gold_m15 = fetch_yahoo_direct(\"GC=F\", range_str=\"10d\", interval_str=\"15m\")",'')
    text=text.replace("df_gold_h1 = fetch_yahoo_direct(\"GC=F\", range_str=\"60d\", interval_str=\"1h\")",'')
    start='def _sanitize_feed_error'; end='def get_xauusd_execution_price'
    if start in text:
        a=text.index(start); b=text.index(end,a); text=text[:a]+LIVE_BLOCK.strip()+'\n\n'+text[b:]
    else:
        text=replace_top_level_function(text,'fetch_canonical_xauusd_feed',LIVE_BLOCK)
    for forbidden in ('PAXGUSDT','GC=F','ifcmarkets.net','IFC Markets','metals.dev','ARGENT_API_KEY','METALS_DEV_API_KEY'):
        if forbidden.lower() in text.lower(): raise RuntimeError(f'forbidden legacy source remains: {forbidden}')
    if 'https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X' not in text: raise RuntimeError('Yahoo Spot endpoint missing')
    BOT.write_text(text,encoding='utf-8')

def tests():
    subprocess.run(['python3','-m','py_compile','bot.py'],check=True); subprocess.run(['git','diff','--check'],check=True)
    text=BOT.read_text(encoding='utf-8'); tree=ast.parse(text)
    wanted={'_build_missing_yahoo_feed','_yahoo_live_error_message','_log_yahoo_live_failure','_set_yahoo_live_cooldown','_yahoo_live_cooldown_active','_build_yahoo_live_feed','fetch_canonical_xauusd_feed','get_xauusd_execution_price','validate_trade_levels','_calculate_realized_r','evaluate_trade_lifecycle'}
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in wanted]
    class L:
        def warning(self,*a): pass
        def error(self,*a): pass
    class T(Exception): pass
    class RExc(Exception): pass
    class Resp:
        def __init__(self,s,p=None,t=''): self.status_code=s; self._p=p; self.text=t
        def json(self):
            if isinstance(self._p,BaseException): raise self._p
            return self._p
    class Req:
        Timeout=T; RequestException=RExc
        def __init__(self,items): self.items=list(items); self.calls=[]
        def get(self,url,params=None,headers=None,timeout=None): self.calls.append((url,params,headers,timeout)); x=self.items.pop(0); 
    # test feed builder/lifecycle without external dependencies
    ns={'math':math,'datetime':datetime,'timezone':timezone,'time':time,'threading':threading,'logger':L(),'PRICE_FEED_STALE_SECONDS':90,'PRICE_FEED_MIN_REQUEST_INTERVAL':60.0,'PRICE_FEED_LOCK':threading.Lock(),'CANONICAL_XAUUSD_FEED_CACHE':{'feed':None,'last_request_monotonic':0.0},'YAHOO_COOLDOWN_SECONDS':1800,'YAHOO_FEED_STATE':{'blocked_until':0.0,'error_type':None,'error_message':None},'YAHOO_STATE_LOCK':threading.Lock()}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'bot.py','exec'),ns)
    now=datetime.now(timezone.utc); payload={'chart':{'result':[{'meta':{'symbol':'XAUUSD=X','regularMarketPrice':3340.25,'regularMarketTime':int(now.timestamp())}}],'error':None}}
    f=ns['_build_yahoo_live_feed'](payload,fetched_at=now); assert f['provider']=='Yahoo Finance' and f['symbol']=='XAUUSD' and f['status']=='ACTIVE' and f['mid']==3340.25 and f['spread_available'] is False
    validate=ns['validate_trade_levels']; calc=ns['_calculate_realized_r']; life=ns['evaluate_trade_lifecycle']; assert validate('BUY',100,95,110,120)[0] and validate('SELL',100,105,90,80)[0]
    assert abs(calc('BUY',100,95,110)-2.0)<1e-9 and abs(calc('SELL',100,105,90)-2.0)<1e-9
    assert life('BUY','OPEN',100,95,110,120,price=111)==[('TP1_HIT',110.0)] and life('BUY','TP1_HIT',100,95,110,120,price=121)==[('TP2_HIT',121.0)]
    assert life('SELL','OPEN',100,105,90,80,price=89)==[('TP1_HIT',90.0)] and life('SELL','TP1_HIT',100,105,90,80,price=79)==[('TP2_HIT',79.0)]
    assert 'trade_outcome:{trade_id}' in text and 'slippage=NULL' in text
    subprocess.run(['git','diff','--check'],check=True); print('YAHOO_SPOT_AND_PHASE1_TESTS_OK')

if __name__=='__main__':
    patch(); tests()
    subprocess.run(['git','diff','--','bot.py'],check=True)
    subprocess.run(['git','config','user.name','github-actions[bot]'],check=True); subprocess.run(['git','config','user.email','41898282+github-actions[bot]@users.noreply.github.com'],check=True)
    subprocess.run(['git','add','bot.py'],check=True); subprocess.run(['git','diff','--cached','--check'],check=True)
    for p in (SCRIPT1,SCRIPT2,WORKFLOW):
        if p.exists(): subprocess.run(['git','rm','-f',str(p)],check=True)
    subprocess.run(['git','diff','--cached','--check'],check=True)
    subprocess.run(['git','commit','-m','fix: restore Yahoo XAUUSD spot feed'],check=True)
    subprocess.run(['git','push','origin','HEAD:main'],check=True)

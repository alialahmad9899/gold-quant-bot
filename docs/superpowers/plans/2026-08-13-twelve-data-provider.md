# Twelve Data Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Yahoo/ArgentAPI market-data paths with Twelve Data XAU/USD while preserving the existing trading lifecycle and quantitative engines.

**Architecture:** Keep the existing market-data contract in `bot.py`, but replace the provider implementation with Twelve Data REST calls. Enforce a hard free-tier budget with process-local caching: live price every 150 seconds, M15 every 30 minutes, H1 every 2 hours, and no uncontrolled retries. Remove Yahoo/yfinance/ArgentAPI market-data code and auxiliary Yahoo DXY/US10Y calls.

**Tech Stack:** Python 3.12+, `requests`, pandas, existing Flask/Telegram/PostgreSQL stack, Twelve Data REST API.

## Global Constraints

- Twelve Data API key is `TWELVE_DATA_API_KEY` and must never be committed.
- Twelve Data symbol is `XAU/USD`.
- Free-plan budget is treated as a hard ceiling of 800 API calls/credits per day and 8 credits/minute.
- Target normal usage is approximately 636 calls/day: 576 live calls + 48 M15 refreshes + 12 H1 refreshes.
- Historical intervals are exactly `15min` and `1h`.
- HTTP 429 enters cooldown; do not retry aggressively.
- No Yahoo Finance, yfinance, ArgentAPI, GC=F, PAXGUSDT, scraping, or synthetic fallback price remains in market data.
- Preserve BUY/SELL execution logic, TP1/TP2/SL, Realized R, historical evaluator, HMM, ML, database schema, and Telegram behavior.

---

### Task 1: Add provider regression tests before implementation

**Files:**
- Modify: `tests/test_market_provider_hygiene.py`

**Interfaces:**
- Produces regression assertions that fail on the current Yahoo implementation and pass only after the Twelve Data migration.

- [ ] **Step 1: Write the failing assertions**

Add assertions requiring `TWELVE_DATA_API_KEY`, `XAU/USD`, `time_series`, `15min`, `1h`, a daily budget constant, and absence of Yahoo/yfinance/ArgentAPI identifiers.

- [ ] **Step 2: Run the test and verify RED**

Run:
```bash
python3 tests/test_market_provider_hygiene.py
```
Expected: FAIL because current `bot.py` still contains Yahoo provider state and the old Yahoo symbol.

---

### Task 2: Replace the market-data provider in `bot.py`

**Files:**
- Modify: `bot.py`

**Interfaces:**
- `fetch_canonical_xauusd_feed()` returns the existing quote dictionary contract.
- `fetch_live_spot_gold()` returns the active mid/spot as a float or `0.0` when unavailable.
- `get_market_data()` continues returning `gold`, `dxy`, `us10y`, and `price_feed` without changing consumers.
- `get_chart_data_cached()` continues returning `df_gold_m15`, `df_gold_h1`, `df_dxy_m15`, `df_us10y_m15`, and `last_fetch`.

- [ ] **Step 1: Replace provider state constants**

Replace Yahoo state/constants with Twelve Data equivalents:
```python
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
TWELVE_DATA_BASE_URL = "https://api.twelvedata.com"
TWELVE_DATA_SYMBOL = "XAU/USD"
TWELVE_DATA_DAILY_LIMIT = 800
TWELVE_DATA_CALLS_PER_MINUTE = 8
TWELVE_DATA_LIVE_INTERVAL_SECONDS = 150
TWELVE_DATA_M15_REFRESH_SECONDS = 1800
TWELVE_DATA_H1_REFRESH_SECONDS = 7200
TWELVE_DATA_RETRY_COOLDOWN_SECONDS = 300
```

- [ ] **Step 2: Implement the cached request helper**

Create a single helper that performs a Twelve Data request, classifies HTTP 429/4xx/5xx/network errors, and records a cooldown. It must not retry in a loop and must reject execution when the daily request budget has been exhausted.

- [ ] **Step 3: Implement live XAU/USD**

Use Twelve Data `/price?symbol=XAU/USD&apikey=...` with the existing cache/gate. Store the response price as `spot` and `mid`. Preserve `bid`/`ask` as `None` unless the provider response explicitly includes them; do not synthesize a spread.

- [ ] **Step 4: Implement historical M15/H1**

Use `/time_series`:
```text
interval=15min
interval=1h
outputsize=...
timezone=UTC
```
Parse `values[].datetime/open/high/low/close/volume` into the existing UTC-indexed pandas DataFrame.

- [ ] **Step 5: Enforce the daily budget**

Persist process-local counters keyed to the UTC date. Before every outbound API call:
```python
if daily_calls >= TWELVE_DATA_DAILY_LIMIT:
    return cached_result_or_error
```
Also enforce the 8-credits/minute limit with a monotonic minute window and serialize requests with a lock.

- [ ] **Step 6: Remove Yahoo auxiliary market calls**

Remove calls to `DX-Y.NYB` and `^TNX` so no Yahoo request remains. Keep `dxy` and `us10y` as `None` when unavailable and preserve the existing downstream fields so HMM/ML code does not break.

- [ ] **Step 7: Remove legacy provider code and imports**

Remove `curl_cffi`, Yahoo-specific helpers/state, `XAUUSD=X`, `YAHOO_*`, `ARGENT_*`, `api.argentapi.com`, and any yfinance fallback. Keep `requests` because the bot already uses it for its local Flask health check.

- [ ] **Step 8: Adjust background polling**

Change `background_cache_worker()` from a 5-second loop to a 30-second loop. The provider-level gates still enforce the 150/1800/7200-second API schedule.

---

### Task 3: Update runtime configuration and documentation

**Files:**
- Modify: `requirements.txt`
- Modify: `README.md`

**Interfaces:**
- Runtime expects `TWELVE_DATA_API_KEY` in Render.

- [ ] **Step 1: Remove the obsolete `curl_cffi` dependency**

Delete the line from `requirements.txt`.

- [ ] **Step 2: Document the Twelve Data environment variable and quota policy**

Document:
```text
TWELVE_DATA_API_KEY
```
and the normal usage budget of approximately 636 calls/day.

- [ ] **Step 3: Document Render setup**

State that the API key must be configured as a Render secret/environment variable and never stored in Git.

---

### Task 4: Run regression and syntax verification

**Files:**
- Test: `tests/test_market_provider_hygiene.py`
- Verify: `bot.py`

- [ ] **Step 1: Run provider hygiene test**

```bash
python3 tests/test_market_provider_hygiene.py
```
Expected: PASS.

- [ ] **Step 2: Run syntax compilation**

```bash
python3 -m py_compile bot.py
```
Expected: exit code 0.

- [ ] **Step 3: Verify no legacy references**

Search `bot.py`, `README.md`, and `requirements.txt` for:
```text
Yahoo
YAHOO_
yfinance
XAUUSD=X
ARGENT
ARGENT_API_KEY
api.argentapi.com
curl_cffi
GC=F
PAXGUSDT
```
Expected: no market-provider references remain.

- [ ] **Step 4: Inspect the final diff**

Confirm only provider/config/documentation code changed; no trade lifecycle, TP/SL, Realized R, HMM, ML, or database schema changes are introduced.

- [ ] **Step 5: Commit and push to `main`**

Commit message:
```text
feat: migrate market data to Twelve Data
```

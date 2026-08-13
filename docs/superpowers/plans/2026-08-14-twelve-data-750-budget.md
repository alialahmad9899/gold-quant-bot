# Twelve Data 750-Credit Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Twelve Data request pass through one quota gate that caps background usage at 750 credits/day, preserves 50 credits for manual actions, and never exceeds the provider's 800-credit daily ceiling.

**Architecture:** Keep the existing `bot.py` structure but replace endpoint-local throttles with a centralized quota state and request wrapper. Quote and OHLC functions remain the public data APIs used by the rest of the bot, while their actual HTTP calls move behind the shared gateway. Cache state becomes per-resource/per-timeframe, and failures enter shared backoff so separate workers cannot amplify retries.

**Tech Stack:** Python 3, requests, asyncio/threading, pandas, pytest, GitHub Actions.

## Global Constraints

- Twelve Data total daily ceiling: 800 credits.
- Background/automatic daily budget: 750 credits.
- Manual reserve: 50 credits.
- Combined minute budget: 4 credits/minute.
- All outbound Twelve Data HTTP attempts are locally counted before the request is sent.
- No direct Twelve Data HTTP call may bypass the centralized quota gateway.
- Existing trading/learning/Gemini behavior remains functionally unchanged except for data-request throttling and cache semantics.

---

### Task 1: Add failing quota-gateway tests

**Files:**
- Modify: `tests/test_twelve_data_guardrails.py`
- Test: `tests/test_twelve_data_guardrails.py`

**Interfaces:**
- Produces tests for `_twelve_data_request_allowed`, `_record_twelve_data_credit`, `_record_twelve_data_failure`, `twelve_data_quota_summary`, and request-class behavior.

- [ ] **Step 1: Write the failing tests**

Add tests asserting:
```python
def test_background_stops_at_750(monkeypatch):
    module._reset_twelve_data_budget_for_tests()
    monkeypatch.setattr(module, "TWELVE_DATA_DAILY_BUDGET", 750)
    module.TWELVE_DATA_STATE["daily_requests"] = 750
    assert not module._twelve_data_request_allowed("quote", request_class="background")


def test_manual_can_use_reserved_50(monkeypatch):
    module._reset_twelve_data_budget_for_tests()
    monkeypatch.setattr(module, "TWELVE_DATA_DAILY_BUDGET", 750)
    module.TWELVE_DATA_STATE["daily_requests"] = 750
    assert module._twelve_data_request_allowed("quote", request_class="manual")


def test_total_hard_ceiling_is_800(monkeypatch):
    module._reset_twelve_data_budget_for_tests()
    module.TWELVE_DATA_STATE["daily_requests"] = 799
    assert module._twelve_data_request_allowed("quote", request_class="manual")
    module._record_twelve_data_credit("manual")
    assert not module._twelve_data_request_allowed("quote", request_class="manual")


def test_failed_request_enters_shared_backoff(monkeypatch):
    module._reset_twelve_data_budget_for_tests()
    now = 1000.0
    monkeypatch.setattr(module.time, "monotonic", lambda: now)
    module._record_twelve_data_failure("429")
    assert module.TWELVE_DATA_STATE["blocked_until"] > now


def test_manual_cached_read_does_not_consume_credit():
    # Exercise the public quote path with a fresh cache and assert the quota counter is unchanged.
    ...
```

Replace the final ellipsis with the repository's actual cache setup and mock transport before running the test.

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_twelve_data_guardrails.py -q
```
Expected: FAIL because the current code has no request-class-aware centralized budget gateway.

- [ ] **Step 3: Commit test changes**

```bash
git add tests/test_twelve_data_guardrails.py
git commit -m "test: define 750-credit background budget and 50-credit manual reserve"
```

---

### Task 2: Implement centralized Twelve Data accounting and backoff

**Files:**
- Modify: `bot.py` in the Twelve Data configuration/state section.

**Interfaces:**
- Produces `TWELVE_DATA_TOTAL_BUDGET=800`, `TWELVE_DATA_BACKGROUND_BUDGET=750`, `TWELVE_DATA_MANUAL_RESERVE=50`, `TWELVE_DATA_MINUTE_BUDGET=4`, `TWELVE_DATA_STATE`, `_twelve_data_request_allowed(request_kind, request_class="background")`, `_record_twelve_data_credit(request_class="background")`, `_record_twelve_data_failure(error_message)`, `_record_twelve_data_success()`, and `twelve_data_quota_summary()`.

- [ ] **Step 1: Add the smallest implementation that makes the budget tests pass**

Use a single lock-protected state object with UTC-day rollover, minute timestamps, blocked-until, backoff, last error, and daily request count. For `background`, deny when `daily_requests >= 750`. For `manual`, deny when `daily_requests >= 800`. For both, deny while blocked or when four credits have already been used in the last 60 seconds.

- [ ] **Step 2: Run the focused tests**

Run:
```bash
pytest tests/test_twelve_data_guardrails.py -q
```
Expected: PASS for accounting, reservation, hard ceiling, and failure backoff tests.

- [ ] **Step 3: Commit**

```bash
git add bot.py tests/test_twelve_data_guardrails.py
git commit -m "fix: centralize Twelve Data quota accounting"
```

---

### Task 3: Route quote and OHLC requests through the gateway

**Files:**
- Modify: `bot.py` in `fetch_twelve_data_live_quote` and `fetch_twelve_data_ohlc`.

**Interfaces:**
- `fetch_twelve_data_live_quote(request_class="background")` remains callable without arguments by existing callers and becomes quota-gated.
- `fetch_twelve_data_ohlc(symbol="XAU/USD", interval="15min", outputsize=150, request_class="background")` remains backward compatible.

- [ ] **Step 1: Update quote accounting**

Before any outbound HTTP call, call `_twelve_data_request_allowed("quote", request_class)` and then `_record_twelve_data_credit(request_class)` immediately before `requests.get`. Record 4xx/5xx/transport failures in `_record_twelve_data_failure` and successful responses in `_record_twelve_data_success`. Do not update the last-success timestamp on failure.

- [ ] **Step 2: Update OHLC accounting**

Use an independent per-key timestamp map such as `TWELVE_DATA_OHLC_LAST_REQUESTS[(symbol, interval)]`. Gate each actual request through the same centralized quota functions. Record attempts before HTTP and enter shared backoff on failures.

- [ ] **Step 3: Run focused tests**

Run:
```bash
pytest tests/test_twelve_data_guardrails.py tests/test_market_cache_refresh.py -q
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add bot.py tests/test_twelve_data_guardrails.py tests/test_market_cache_refresh.py
git commit -m "fix: route Twelve Data quote and OHLC through quota gateway"
```

---

### Task 4: Fix canonical feed caching and historical refresh amplification

**Files:**
- Modify: `bot.py` in `fetch_canonical_xauusd_feed`, `get_chart_data_cached`, and related feed helpers.
- Test: `tests/test_market_cache_refresh.py`, `tests/test_market_feed_semantics.py`.

**Interfaces:**
- Canonical XAU/USD feed reuses a fresh cached quote without spending a credit.
- Historical cache stores independent H1/M15 refresh timestamps.

- [ ] **Step 1: Add failing cache tests**

Assert that a failed H1/M15 fetch does not cause an immediate repeated request on every caller, and that H1 and M15 have independent refresh intervals.

- [ ] **Step 2: Implement per-resource cache metadata**

Track last successful fetch and last attempted fetch per `(symbol, timeframe)`. On failure, preserve the previous valid dataframe and rely on the shared backoff rather than invalidating the refresh clock. Do not derive a live bid/ask from an M15 close.

- [ ] **Step 3: Run tests**

Run:
```bash
pytest tests/test_market_cache_refresh.py tests/test_market_feed_semantics.py -q
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add bot.py tests/test_market_cache_refresh.py tests/test_market_feed_semantics.py
git commit -m "fix: stop Twelve Data cache refresh amplification"
```

---

### Task 5: Mark manual user paths and preserve the 50-credit reserve

**Files:**
- Modify: `bot.py` in `price`, `analyze`, `signal`, `ai_info`, `backtest`, and `system_health_check` call paths as needed.

**Interfaces:**
- Manual user-triggered market/data fetches use `request_class="manual"`.
- Background workers continue using `request_class="background"`.

- [ ] **Step 1: Add failing manual-reserve tests**

Mock a fresh cache and an uncached manual request and assert one credit is consumed from total capacity but the background budget remains protected.

- [ ] **Step 2: Implement explicit request-class propagation**

Pass `request_class="manual"` only when a user action truly needs an outbound Twelve Data request. Preserve cache-first semantics so a manual button hit does not automatically spend a credit when a fresh cache already exists.

- [ ] **Step 3: Run focused tests**

Run:
```bash
pytest tests/test_twelve_data_guardrails.py tests/test_market_cache_refresh.py -q
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add bot.py tests/test_twelve_data_guardrails.py tests/test_market_cache_refresh.py
git commit -m "fix: reserve final 50 Twelve Data credits for manual actions"
```

---

### Task 6: Remove direct Twelve Data bypasses and verify the whole repository

**Files:**
- Modify: `bot.py` only if a direct bypass remains.
- Test: full repository test suite.

**Interfaces:**
- No direct Twelve Data `requests.get()` remains outside the centralized fetch gateway.

- [ ] **Step 1: Search for bypasses**

Run:
```bash
grep -nE 'api\.twelvedata\.com|twelvedata\.com' bot.py
```
Expected: only the centralized quote/time-series gateway URLs remain.

- [ ] **Step 2: Run syntax and tests**

Run:
```bash
python3 -m py_compile bot.py
pytest -q
```
Expected: no syntax failures and all tests pass.

- [ ] **Step 3: Run diff validation**

Run:
```bash
git diff --check
```
Expected: no whitespace errors.

- [ ] **Step 4: Commit final verification changes**

```bash
git add bot.py tests
 git commit -m "test: verify Twelve Data 750-credit quota architecture"
```

- [ ] **Step 5: Push main**

```bash
git push origin main
```

- [ ] **Step 6: Verify remote head**

Confirm the pushed `main` commit SHA and inspect the final file content from GitHub before reporting completion.

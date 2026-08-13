# Twelve Data Free Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the existing Gold Quant Bot so Twelve Data remains the only market-data provider, Basic/free quota is conserved, stale/historical data cannot masquerade as live execution prices, and the existing Telegram/ML/Gemini/trade-lifecycle architecture remains intact.

**Architecture:** Keep `bot.py` as the application entrypoint and improve its existing Twelve Data/cache/health paths rather than restructuring the bot. Separate live-quote health from historical-candle availability, add a daily/minute quota budget with failure backoff, block new trades when no valid live quote exists, and preserve all Telegram user-facing messages in Arabic. Add regression tests and correct dependency/documentation drift.

**Tech Stack:** Python, requests, pandas, numpy, scikit-learn, Twelve Data REST API, PostgreSQL/SQLite, python-telegram-bot, Gemini SDK.

## Global Constraints

- Twelve Data is the only market-data provider for XAU/USD; do not add Yahoo Finance, futures, crypto proxies, scraping, or another vendor.
- Keep the current Telegram bot architecture, signal engine, SMC/HMM/ML/Gemini flow, database model, and trade lifecycle semantics intact except for necessary correctness fixes.
- Never treat an M15/H1 candle close as a live execution quote.
- No new trade may be created without a valid, sufficiently fresh Twelve Data live quote.
- Historical analysis may continue from valid cached Twelve Data candles when live execution data is unavailable.
- Protect the Basic/free allowance with explicit minute/day request budgeting and backoff; target a conservative daily budget below the published 800-credit/day allowance.
- All Telegram/user-facing messages remain Arabic; technical diagnostics may remain in logs.
- Verify compilation, regression tests, and targeted quota/lifecycle behavior before claiming completion.

---

### Task 1: Twelve Data quota guardrails and request coordination

**Files:**
- Modify: `bot.py` around Twelve Data interval state and request functions.
- Test: `tests/test_twelve_data_guardrails.py`

**Interfaces:**
- Preserve `fetch_twelve_data_live_quote()` and `fetch_twelve_data_ohlc()` signatures.
- Add small internal helpers for credit accounting/backoff without changing callers.

- [ ] Write tests for successful-request accounting, minute throttling, daily conservation mode, and daily exhaustion behavior.
- [ ] Run the targeted tests and confirm they fail against the current implementation.
- [ ] Implement a process-local quota ledger with conservative daily target, minute request timestamp, successful/attempted request accounting, and reset logic.
- [ ] Add separate handling for minute throttling versus daily exhaustion, with exponential/backoff cooldown after failures including 429/error responses.
- [ ] Ensure a failed request cannot create an immediate retry storm merely because the previous successful timestamp is unchanged.
- [ ] Keep cache reads free of API calls and ensure repeated consumers share one Twelve Data request window.
- [ ] Run targeted quota tests until green.

### Task 2: Correct live-vs-historical feed semantics

**Files:**
- Modify: `bot.py` around `_build_missing_twelve_feed`, `_refresh_cached_feed_status`, `fetch_twelve_data_live_quote`, `fetch_canonical_xauusd_feed`, `get_xauusd_execution_price`.
- Test: `tests/test_market_feed_semantics.py`

**Interfaces:**
- Preserve returned feed dictionaries and existing execution-price API where possible.
- Add explicit live/historical source metadata only as additive fields.

- [ ] Write failing tests proving an M15 close cannot be returned as `ACTIVE` live execution data and that its real candle timestamp controls staleness.
- [ ] Run the tests and observe the expected failures.
- [ ] Remove the current M15-close-to-live-price behavior.
- [ ] Make live quote validity depend on a real Twelve Data live response and a real source timestamp.
- [ ] Preserve cached live quotes only while their true source age is within the configured threshold; otherwise mark them `STALE` and block execution.
- [ ] Do not manufacture bid/ask from close; absent bid/ask must not be represented as a real spread.
- [ ] Keep historical candles available for analysis independently from live execution availability.
- [ ] Run the feed-semantic tests until green.

### Task 3: Intelligent historical cache refresh

**Files:**
- Modify: `bot.py` around `fetch_twelve_data_ohlc`, `get_chart_data_cached`, and scanner/cache workers.
- Test: `tests/test_market_cache_refresh.py`

**Interfaces:**
- Preserve `get_chart_data_cached()` return structure.
- Preserve H1/M15 analysis consumers.

- [ ] Write failing tests for cache reuse within a candle window, M15 refresh on a new candle, H1 refresh on a new H1 boundary, and API-error backoff.
- [ ] Verify the tests fail against the current unconditional refresh behavior.
- [ ] Implement candle-aware refresh decisions using the last valid Twelve Data candle timestamp instead of blind polling alone.
- [ ] Keep the existing cache locks and memory protections.
- [ ] Ensure initial bootstrap can fetch the larger sample set needed by the HMM/ML engine, while subsequent updates are conservative.
- [ ] Run targeted cache tests until green.

### Task 4: Trade execution guard without weakening analysis

**Files:**
- Modify: `bot.py` around `generate_quant_signal`, `get_market_data`, and feed status helpers.
- Test: `tests/test_signal_execution_guard.py`

**Interfaces:**
- Preserve existing signal dictionary and lifecycle fields.
- Preserve BUY/SELL validation and TP1/TP2/SL lifecycle.

- [ ] Write failing tests proving the analyzer can operate on cached historical data while signal creation returns `WAIT` when live quote is missing/stale.
- [ ] Run tests and verify they fail against current M15 fallback behavior.
- [ ] Add a strict execution gate using live Twelve Data quote freshness and valid execution-side price.
- [ ] Keep analysis available in `/analyze` and backtest paths when historical cache is healthy.
- [ ] Ensure a blocked live feed cannot create a trade row or Learning Event.
- [ ] Run targeted signal tests until green.

### Task 5: Dependency and documentation hygiene

**Files:**
- Modify: `requirements.txt`
- Modify: `README.md`
- Test: `tests/test_project_hygiene.py`

**Interfaces:**
- No runtime behavior change beyond ensuring declared dependencies match imports.

- [ ] Add `scikit-learn` to runtime requirements.
- [ ] Update README to state Twelve Data is the sole XAU/USD market-data provider and explain the free-plan guardrails without claiming unavailable Commodity access.
- [ ] Document the Arabic Telegram-message requirement and the distinction between cached historical analysis and live execution eligibility.
- [ ] Remove references to deleted/nonexistent test files and describe the actual validation commands.
- [ ] Add hygiene checks for required dependency declarations and repository test paths.
- [ ] Run hygiene tests.

### Task 6: Regression suite and verification

**Files:**
- Create/Modify: `tests/` files created by Tasks 1-5.

- [ ] Run `python3 -m py_compile bot.py`.
- [ ] Run the full test suite with `python3 -m pytest -q`.
- [ ] Run a targeted static search to confirm no Yahoo Finance/XAUUSD fallback strings remain in the production market-data path.
- [ ] Review the final diff for Telegram-facing Arabic text and confirm no English replacement messages were introduced.
- [ ] Verify the final branch contains only intended files and no secrets.
- [ ] Commit the implementation with a focused message and push the verified changes to `main`.

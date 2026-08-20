# Phase 2 Active Trade Intelligence Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the existing Active Trade Intelligence layers into the live XAUUSD signal pipeline so one active position is enforced, thesis/state persist across restarts, and every active trade is reviewed for TP1, invalidation, reversal, and dynamic management.

**Architecture:** Keep `generate_quant_signal()` as the existing quantitative/SMC/HMM/ML/Gemini decision engine. Add lifecycle persistence and review orchestration in `trade_intelligence.py`; wire the shared manager into `bot.py` only at signal acceptance and active-trade monitoring. The existing `trades` SQL table remains the execution/outcome ledger; Twelve Data remains the only XAUUSD provider.

**Tech Stack:** Python 3.11, dataclasses/enums, JSON state persistence, existing SQLite/PostgreSQL ledger, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-20-active-trade-intelligence-integration-design.md`

## Global Constraints

- Twelve Data remains the only XAU/USD provider.
- Existing SMC/HMM/ML/Gemini signal logic is preserved.
- Telegram-facing messages remain Arabic.
- No Yahoo/fallback gold provider is added.
- Active position lock blocks duplicate live positions across directions.
- Lifecycle state survives restart through the existing JSON state layer.

---

### Task 1: Lifecycle state and persistence contract

**Files:**
- Modify: `trade_intelligence.py`
- Modify: `trade_state_store.py`
- Test: `tests/test_trade_intelligence_phase2.py`

**Interfaces:**
- `TradeIntelligence(store: TradeStateStore | None = None)`
- `open_trade(thesis: TradeThesis) -> bool`
- `review(market: dict) -> ReviewDecision`
- `manage(market: dict) -> dict`
- `close(reason: str) -> None`
- `snapshot() -> dict`

- [ ] **Step 1: Add failing tests for persisted open state, thesis fields, and restore.**
- [ ] **Step 2: Run the focused pytest and verify the new assertions fail because persistence is not wired.**
- [ ] **Step 3: Implement serialization/deserialization using `TradeStateStore`.**
- [ ] **Step 4: Re-run the focused tests and confirm they pass.**

### Task 2: Invalidation, reversal, and dynamic review

**Files:**
- Modify: `trade_intelligence.py`
- Test: `tests/test_trade_intelligence_phase2.py`

**Interfaces:**
- `review()` consumes `invalidation`, `structure_break_against`, `reversal_signal`, `temporary_pressure`, and `tp1_reached`.
- `manage()` returns `decision`, `state`, and deterministic management flags.

- [ ] **Step 1: Add failing tests for invalidation, reversal, TP1, pressure, and keep transitions.**
- [ ] **Step 2: Run them and verify the lifecycle transition behavior is currently incomplete.**
- [ ] **Step 3: Implement priority order invalidation → reversal → TP1 → pressure → keep and persist each transition.**
- [ ] **Step 4: Re-run focused tests and confirm green.**

### Task 3: Signal pipeline integration and position lock

**Files:**
- Modify: `bot.py`
- Test: `tests/test_trade_intelligence_integration.py`

**Interfaces:**
- `build_trade_thesis(candidate_signal: dict, market_summary: dict, smc: dict) -> TradeThesis`
- `review_active_trade(market: dict) -> dict`
- Shared lifecycle manager is initialized once at module scope.

- [ ] **Step 1: Add failing integration tests proving a second opposite-direction signal is rejected while a phase-2 active trade exists.**
- [ ] **Step 2: Run them and verify they fail before the bot is wired to the manager.**
- [ ] **Step 3: Wire the shared manager into `generate_quant_signal()` immediately after Gemini approval and before final ledger notification.**
- [ ] **Step 4: Ensure the existing SQL duplicate/candle guards remain in place as a second safety layer.**
- [ ] **Step 5: Run focused integration tests and confirm green.**

### Task 4: Active-trade review and synchronization

**Files:**
- Modify: `bot.py`
- Modify: `trade_intelligence.py`
- Test: `tests/test_trade_intelligence_integration.py`

- [ ] **Step 1: Add failing tests for review output and synchronization after TP1/final close.**
- [ ] **Step 2: Implement monitor-side lifecycle review using the current market/technical snapshot without adding data providers.**
- [ ] **Step 3: Synchronize lifecycle state with existing SQL TP1/TP2/SL events and clear the active phase-2 position only when the ledger is finalized.**
- [ ] **Step 4: Re-run focused tests and confirm green.**

### Task 5: Full regression and CI verification

**Files:**
- Modify: `tests/test_trade_intelligence_phase2.py`
- Modify: `tests/test_trade_intelligence_integration.py`

- [ ] **Step 1: Run the complete pytest suite.**
- [ ] **Step 2: Fix regressions caused by this integration only.**
- [ ] **Step 3: Run `python -m py_compile bot.py trade_intelligence.py trade_state_store.py`.**
- [ ] **Step 4: Run `git diff --check`.**
- [ ] **Step 5: Commit the verified implementation.**
- [ ] **Step 6: Push the feature branch, create a PR to `main`, verify CI, and merge to `main`.**

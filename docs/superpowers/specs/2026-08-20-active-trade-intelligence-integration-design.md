# Phase 2 Active Trade Intelligence Integration Design

## Context
The repository already contains separate Active Trade Intelligence lifecycle code, a persistent state helper, an execution guard, and runtime tests. The live `bot.py` signal pipeline, however, still performs its own SQL duplicate check and writes the trade ledger without registering the accepted signal in the lifecycle engine.

## Goals
1. Make the lifecycle engine participate in signal acceptance.
2. Enforce one active position across BUY/SELL directions.
3. Persist thesis, state, and transition history across process restarts.
4. Review active trades for invalidation, reversal, TP1, and temporary pressure.
5. Keep the existing quantitative signal scoring and Twelve Data feed unchanged.
6. Keep the SQL `trades` table as the source of execution/outcome history.

## Design
`TradeIntelligence` becomes the canonical in-process lifecycle coordinator and receives a `TradeStateStore` instance. Its serialized snapshot contains the active trade thesis, current state, creation time, and transition history. The store writes the current state to the existing environment-selected JSON file.

`generate_quant_signal()` keeps all current SMC/HMM/ML/Gemini calculations. Only after the signal passes those checks and Gemini review does the pipeline build a `TradeThesis` and ask `TradeIntelligence.open_trade()`. A false return means an active position already exists and the pipeline returns WAIT without creating another ledger record or Telegram alert.

`monitor_open_trades()` remains responsible for real execution-price lifecycle events already represented in SQL. It also feeds deterministic structure/reversal/pressure flags to the lifecycle manager. SQL outcome changes synchronize the lifecycle manager so persistence and the existing ledger remain aligned.

## Review priority
Invalidation has highest priority, followed by an explicit reversal signal, TP1 reached, temporary pressure, then KEEP. Invalidation returns `REVERSE` at the decision layer so the caller can distinguish a broken thesis from ordinary profit-taking.

## Non-goals
No new market-data provider, no Yahoo fallback, no replacement of SMC/HMM/ML/Gemini, no broker execution integration, and no removal of the existing SQL ledger/lifecycle functions.

## Testing
Tests cover state persistence, active-position locking, thesis contents, each review transition, dynamic management output, synchronization after close, and integration with the existing signal path. CI continues to run the full pytest suite, `py_compile`, and `git diff --check`.

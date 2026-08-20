# Institutional Trade Review

The signal pipeline applies a deterministic institutional-style risk gate after the existing quantitative/SMC/HMM/ML/Gemini signal is produced and before Phase 2 opens an active trade.

The gate evaluates hard vetoes, market regime, structure/trend alignment, RR, SL/TP direction, momentum, DXY correlation, historical lessons, counter-trade risk, thesis, invalidation and reversal conditions.

Gemini may add an adversarial second opinion when deterministic rules find no hard veto. Gemini cannot override deterministic hard vetoes, while a Gemini rejection can veto an otherwise clean candidate.

Runtime controls:
- `INSTITUTIONAL_MIN_RR` (default `1.50`)
- `INSTITUTIONAL_MIN_CONFIDENCE` (default `45`)
- `INSTITUTIONAL_APPROVE_SCORE` (default `72`)
- `INSTITUTIONAL_MODIFY_SCORE` (default `58`)
- `INSTITUTIONAL_GEMINI_REVIEW` (default `1`)
- `INSTITUTIONAL_AI_MIN_SCORE` (default `58`)

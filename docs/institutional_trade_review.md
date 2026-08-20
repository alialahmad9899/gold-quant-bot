# Institutional Trade Review

The signal pipeline now applies a deterministic institutional-style risk gate after the existing quantitative/SMC/HMM/ML/Gemini signal is produced and before the Phase 2 active trade is opened.

The gate evaluates hard vetoes, market regime, structure/trend alignment, RR, momentum, DXY correlation, historical lessons, counter-trade risk, thesis, invalidation and reversal conditions.

Gemini may add an adversarial second opinion when deterministic rules find no hard veto. Gemini cannot override deterministic hard vetoes, and a Gemini rejection can veto an otherwise clean candidate.

Runtime controls:
- `INSTITUTIONAL_MIN_RR` (default `1.50`)
- `INSTITUTIONAL_MIN_CONFIDENCE` (default `45`)
- `INSTITUTIONAL_APPROVE_SCORE` (default `72`)
- `INSTITUTIONAL_MODIFY_SCORE` (default `58`)
- `INSTITUTIONAL_GEMINI_REVIEW` (default `1`)
- `INSTITUTIONAL_AI_MIN_SCORE` (default `58`)

# Twelve Data Market Provider Design

## Goal
Replace Yahoo/ArgentAPI market-data paths with Twelve Data for XAU/USD Spot plus M15/H1 OHLC while preserving the existing trade lifecycle and quantitative engines.

## Architecture
Twelve Data is the sole market-data provider. The bot exposes the same internal quote contract (`bid`, `ask`, `mid`, `spot`, `timestamp`, `age_seconds`) and OHLC DataFrame contract used by the existing strategy code. A process-local cache and minimum request interval enforce the free-plan quota.

The free plan is treated as a hard budget of 800 API credits/day and 8 API credits/minute. The implementation targets a conservative ceiling below 800 calls/day: live XAU/USD every 150 seconds (576/day), M15 refresh every 30 minutes (48/day), and H1 refresh every 2 hours (12/day), for approximately 636 successful API calls/day before manual `/health` checks or exceptional retries. No uncontrolled retry loop is allowed.

## Constraints
- Provider: Twelve Data only for market data.
- Symbol: `XAU/USD`.
- API key: `TWELVE_DATA_API_KEY`; never commit the secret.
- Live endpoint: Twelve Data `/price` or `/quote` contract; use the response price as `mid/spot` when bid/ask are unavailable.
- Historical endpoint: `/time_series` for `15min` and `1h`.
- Timestamps: normalize to UTC.
- HTTP 429: enter cooldown and never retry aggressively.
- Yahoo Finance, yfinance, ArgentAPI, GC=F, PAXGUSDT, scraping, synthetic prices: remove from production market-data path.
- DXY and US10Y Yahoo auxiliary requests: remove so no Yahoo dependency remains.
- Preserve BUY/SELL execution-price logic, TP1/TP2/SL, Realized R, historical evaluator, HMM, ML, and database schemas.

## Testing
Regression tests must prove no legacy provider references remain, Twelve Data configuration is present, the hard daily-budget settings exist, and M15/H1 endpoints use `15min`/`1h`.

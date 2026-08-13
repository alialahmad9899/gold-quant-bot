# Twelve Data 750-Credit Budget Design

## Goal
Guarantee that the trading bot never intentionally consumes more than 750 Twelve Data API credits per UTC day from the automatic/background paths, reserving the remaining 50 credits under the 800-credit plan for manual user actions such as live price, market analysis, and AI information requests.

## Architecture
Introduce one centralized Twelve Data quota gateway in `bot.py`. Every outbound Twelve Data request must pass an atomic preflight check that tracks daily credits, minute credits, request type, failure backoff, and the reserved manual pool. Automatic/background requests may consume at most 750 daily credits. Manual requests may consume only from the total remaining capacity after automatic usage plus the protected 50-credit reserve rule, and can never push the aggregate above 800.

Replace endpoint-local success timestamps with per-endpoint cache windows plus centralized accounting. Failed requests count as attempts, enter exponential backoff, and cannot immediately retry from another caller. Historical data keeps independent cache timestamps per symbol/timeframe. Background scanners and cache refreshers must reuse cached snapshots instead of generating duplicate Twelve Data calls.

## Exact Budget Rules
- Twelve Data hard daily ceiling: 800 credits.
- Automatic/background daily budget: 750 credits.
- Protected manual reserve: 50 credits.
- Automatic/background paths are blocked at 750 credits for the current UTC day.
- Manual paths are allowed only while total recorded credits remain below 800.
- The limiter is monotonic and atomic under the existing Twelve Data lock; concurrent callers cannot oversubscribe the same remaining credits.
- Minute budget remains below the provider's 8 credits/minute ceiling; default automatic/manual combined budget is 4 credits/minute to preserve a safety margin.
- Each actual outbound Twelve Data HTTP attempt consumes one credit in the local accounting model before the network call is made.
- 429/403/401/timeouts/transport errors trigger backoff and are not retried immediately by another caller.

## Data Flow
`caller -> centralized quota gateway -> endpoint cache/backoff -> Twelve Data HTTP request -> cache/state update`

All code paths using `fetch_twelve_data_live_quote()` and `fetch_twelve_data_ohlc()` must use the centralized gateway. The canonical XAU/USD feed must not fabricate a live quote from an M15 candle. Historical cache failures must record a failed attempt/backoff and must not leave `last_fetch` in a state that causes a hot retry loop.

## Manual Reserve Semantics
Manual requests are identified explicitly with `request_class="manual"`. Automatic workers use `request_class="background"`. Background traffic can never consume the last 50 credits of the 800-credit daily ceiling. Manual requests may consume those credits. Manual requests also reuse fresh cache entries and therefore do not spend credits when a suitable cached result exists.

## Tests
Add/extend tests covering:
1. 750-credit automatic ceiling.
2. 50-credit protected manual reserve.
3. Aggregate hard ceiling of 800.
4. Atomic concurrent request accounting.
5. Minute budget enforcement.
6. Failed-request backoff for 429 and transport errors.
7. No duplicate quote requests within cache interval.
8. Independent H1/M15 historical cache windows.
9. Historical failure does not create a 30-second/15-second retry storm.
10. Manual cached reads do not consume credits.
11. Manual uncached requests can use the reserved 50 credits.
12. Existing signal, analysis, price and AI-info paths continue to use the shared data layer.

## Verification
Run `python3 -m py_compile bot.py`, the full `pytest -q` suite, and `git diff --check`. Inspect the final diff for any direct Twelve Data `requests.get()` call that bypasses the quota gateway. Confirm the final branch is `main` and the changes are committed and pushed.

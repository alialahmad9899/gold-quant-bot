# Live News Intelligence Design

## Goal
Add a real-time news intelligence subsystem for XAU/USD that collects relevant financial and geopolitical headlines, scores their likely gold impact, confirms the market reaction with the existing Twelve Data price pipeline, and feeds the result into signal generation and active-trade management without making the AI an overly strict gate.

## Architecture
The subsystem is a separate `news_intelligence.py` module. It polls configurable RSS/Atom sources, deduplicates headlines, classifies relevance and gold/USD impact, assigns confidence and urgency, and persists the latest event state. It never supplies gold prices; Twelve Data remains the sole gold market-data provider. News can create a news-driven candidate signal only after price confirmation and the existing risk pipeline, and can trigger immediate re-review of active trades when a high-impact event conflicts with their thesis.

## Sources
Use public RSS/Atom feeds and official economic-release feeds by default, with environment-configurable URLs. The default set must include authoritative U.S. monetary-policy/economic sources plus reputable market/news feeds. Source failures are isolated; one broken feed must not block other feeds or the trading loop. No new gold-price provider is introduced.

## Decision model
Each event produces:
- relevance: 0-100
- gold impact: -100..100 (negative = bearish gold, positive = bullish gold)
- confidence: 0-100
- urgency: LOW/MEDIUM/HIGH/CRITICAL
- event type: MACRO/FED/INFLATION/JOBS/YIELDS/DXY/GEOPOLITICAL/CENTRAL_BANK/OTHER

The engine must distinguish headline direction from actual market reaction. A headline alone is not enough for an immediate trade. For a news-driven entry, the engine requires meaningful news impact, sufficient confidence, valid live price data, and confirming price reaction before passing a candidate to the existing signal/risk pipeline.

## Active trade behavior
A new HIGH/CRITICAL event immediately marks affected active trades for news re-review. The Trade Lawyer evaluates whether the event supports, weakens, or invalidates the current thesis. Normal HOLD behavior remains the default; news does not force an exit unless the existing invalidation/risk rules or a strong multi-signal conflict supports it.

## Telegram behavior
Send Arabic alerts only for materially new/high-impact events or when the news assessment changes an active trade recommendation. Do not spam every polling cycle. Provide a manual news-status command/button for the latest relevant event state and an immediate active-trade news review.

## Flexibility constraints
News intelligence is advisory by default. It must not impose a daily trade quota, force BUY/SELL symmetry, or turn moderate news uncertainty into a hard veto. Hard blocking is reserved for invalid data, clearly contradictory high-impact events with corroborating market reaction, or existing safety gates.

## Testing
Cover feed parsing, deduplication, relevance filtering, impact classification, confidence/urgency calculation, failure isolation, news-driven candidate confirmation, active-trade re-review triggers, Telegram formatting, and the invariant that Twelve Data remains the only gold-price source.

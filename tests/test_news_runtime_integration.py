from datetime import datetime, timezone

from news_intelligence import NewsArticle, NewsIntelligence


def article(title="Fed signals lower rates", summary="Dovish easing and weaker dollar"):
    return NewsArticle(title, summary, "https://example.com/1", "TestFeed", datetime.now(timezone.utc))


def test_poll_latest_evaluates_fresh_news_for_entry_confirmation():
    engine = NewsIntelligence()
    engine.fetch_latest = lambda: [article()]
    result = engine.poll(price_change_pct=0.35, price_direction="UP")
    assert result["fresh_count"] == 1
    assert result["decision"]["action"] == "NEWS_BUY"
    assert result["decision"]["direction"] == "BULLISH_GOLD"


def test_poll_latest_reassesses_active_trade_on_material_conflict():
    engine = NewsIntelligence()
    engine.fetch_latest = lambda: [article("Fed signals emergency rate hike", "Aggressive tightening, higher rates, hawkish policy")]
    result = engine.poll(price_change_pct=-0.4, price_direction="DOWN", active_direction="BUY")
    assert result["fresh_count"] == 1
    assert result["decision"]["conflict"] is True
    assert result["decision"]["action"] in {"REDUCE_RISK", "EXIT"}


def test_poll_does_not_repeat_consumed_news():
    engine = NewsIntelligence()
    calls = {"n": 0}
    def fetch():
        calls["n"] += 1
        return [article()] if calls["n"] == 1 else []
    engine.fetch_latest = fetch
    first = engine.poll(price_change_pct=0.35, price_direction="UP")
    second = engine.poll(price_change_pct=0.35, price_direction="UP")
    assert first["fresh_count"] == 1
    assert second["fresh_count"] == 0


def test_news_entry_candidate_never_overrides_existing_signal_without_confirmation():
    engine = NewsIntelligence()
    engine.fetch_latest = lambda: [article()]
    result = engine.poll(price_change_pct=0.0, price_direction="UP")
    assert result["decision"]["action"] == "WAIT_CONFIRMATION"

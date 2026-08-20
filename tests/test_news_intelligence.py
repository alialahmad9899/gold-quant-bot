from datetime import datetime, timezone

from news_intelligence import NewsArticle, NewsIntelligence, classify_gold_impact


def article(**overrides):
    data = dict(
        title="Fed signals lower rates after softer inflation",
        summary="Officials discuss easing policy as inflation cools.",
        url="https://example.com/fed",
        source="Reuters",
        published_at=datetime.now(timezone.utc),
    )
    data.update(overrides)
    return NewsArticle(**data)


def test_classifies_material_fed_dovish_news_as_bullish_gold():
    result = classify_gold_impact(article())
    assert result.direction == "BULLISH_GOLD"
    assert result.impact >= 60
    assert result.urgency in {"HIGH", "MEDIUM"}


def test_ignores_unrelated_news():
    result = classify_gold_impact(article(title="Local football team wins championship", summary="Sports result"))
    assert result.direction == "NEUTRAL"
    assert result.impact < 30


def test_deduplicates_same_url_and_title():
    engine = NewsIntelligence()
    first = engine.ingest([article()])
    second = engine.ingest([article()])
    assert len(first) == 1
    assert second == []


def test_strong_news_requires_price_confirmation_before_entry():
    engine = NewsIntelligence()
    event = engine.evaluate_news_entry(article(), price_change_pct=0.0, price_direction="UP")
    assert event.action == "WAIT_CONFIRMATION"

    confirmed = engine.evaluate_news_entry(article(), price_change_pct=0.35, price_direction="UP")
    assert confirmed.action == "NEWS_BUY"


def test_news_conflict_can_trigger_active_trade_reassessment():
    engine = NewsIntelligence()
    decision = engine.evaluate_active_trade(
        direction="BUY",
        article=article(title="Fed signals emergency rate hike", summary="Officials signal aggressive tightening and higher rates"),
        price_change_pct=-0.4,
        price_direction="DOWN",
    )
    assert decision.action in {"REDUCE_RISK", "EXIT", "REASSESS"}
    assert decision.conflict is True


def test_news_without_material_gold_impact_does_not_force_trade():
    engine = NewsIntelligence()
    decision = engine.evaluate_news_entry(
        article(title="Company launches new smartphone", summary="Technology company unveils a new device"),
        price_change_pct=1.0,
        price_direction="UP",
    )
    assert decision.action == "NO_TRADE"

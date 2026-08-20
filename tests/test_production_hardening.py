from datetime import datetime, timezone, timedelta

from production_hardening import parse_economic_surprise, price_reaction_from_event, semantic_news_signal


def test_surprise_parsing_actual_vs_forecast():
    result = parse_economic_surprise("CPI Actual: 3.4% Forecast: 3.0% Previous: 3.2%")
    assert result["actual"] == 3.4
    assert result["forecast"] == 3.0
    assert result["previous"] == 3.2
    assert result["surprise"] == 0.4


def test_positive_macro_surprise_is_gold_bearish():
    result = semantic_news_signal("US CPI Actual 3.4 Forecast 3.0", "inflation remains sticky")
    assert result["direction"] == "BEARISH_GOLD"
    assert result["surprise"]["surprise"] == 0.4


def test_negative_macro_surprise_is_gold_bullish():
    result = semantic_news_signal("US CPI Actual 2.7 Forecast 3.0", "inflation cools")
    assert result["direction"] == "BULLISH_GOLD"


def test_context_beats_single_keyword():
    result = semantic_news_signal("Ceasefire reduces geopolitical risk", "gold falls as safe-haven demand fades")
    assert result["direction"] == "BEARISH_GOLD"


def test_neutral_news_is_not_material():
    result = semantic_news_signal("Gold market unchanged", "traders await more data")
    assert result["direction"] == "NEUTRAL"
    assert result["material"] is False


def test_event_price_reaction_is_timestamp_anchored():
    event = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    history = [
        (event - timedelta(seconds=5), 3300.0),
        (event + timedelta(seconds=2), 3300.0),
        (event + timedelta(seconds=30), 3308.0),
    ]
    result = price_reaction_from_event(event, 3308.0, history)
    assert result["baseline"] == 3300.0
    assert result["direction"] == "UP"
    assert result["confirmed"] is True


def test_pre_event_move_is_not_used_as_baseline():
    event = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    history = [(event - timedelta(seconds=10), 3200.0)]
    result = price_reaction_from_event(event, 3300.0, history)
    assert result["confirmed"] is False
    assert result["baseline"] is None

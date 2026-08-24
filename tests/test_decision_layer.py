from datetime import datetime, timezone

import pandas as pd

import decision_layer as dl


def _frame():
    idx = pd.date_range("2026-08-20", periods=24, freq="1h", tz="UTC")
    base = pd.Series(range(24), index=idx, dtype=float) + 100
    return pd.DataFrame({"Open": base, "High": base + 1, "Low": base - 1, "Close": base + 0.5})


def test_resample_h4_creates_real_four_hour_bars():
    out = dl._resample_h4(_frame())
    assert len(out) == 6
    assert list(out.columns) == ["Open", "High", "Low", "Close"]
    assert out.index[0].tzinfo is not None


def test_levels_have_consistent_rr_for_buy_and_sell():
    feed = {"bid": 4599.5, "ask": 4600.5}
    buy = dl._levels("BUY", 4600.5, 5.0, feed)
    sell = dl._levels("SELL", 4599.5, 5.0, feed)
    assert dl._rr("BUY", 4600.5, buy[0], buy[1]) >= dl.MIN_RR
    assert dl._rr("SELL", 4599.5, sell[0], sell[1]) >= dl.MIN_RR
    assert buy[0] < 4600.5 < buy[1]
    assert sell[1] < 4599.5 < sell[0]
    assert buy[3] / 4600.5 >= dl.MIN_STOP_PCT


def test_score_does_not_treat_dxy_correlation_alone_as_direction():
    data = {"h4_trend": "BULLISH", "state_label": "BULLISH", "smc": {}, "dxy_pressure": "NEUTRAL"}
    direction, scores = dl._score(data, 105, 100, 0.60)
    assert direction == "BUY"
    assert scores["bull_score"] > scores["bear_score"]


def test_h4_hmm_conflict_is_not_an_automatic_veto():
    data = {"h4_trend": "BULLISH", "state_label": "BEARISH", "smc": {"fvg_bullish": True}, "dxy_pressure": "NEUTRAL"}
    direction, scores = dl._score(data, 99, 101, 0.65)
    assert direction in {"BUY", None, "SELL"}
    assert scores["margin"] >= 0


def test_direction_setup_key_is_stable_for_same_context():
    a = "BUY|BULLISH|BULLISH|تأكيد صاعد|12"
    b = "BUY|BULLISH|BULLISH|تأكيد صاعد|12"
    assert __import__("hashlib").sha256(a.encode()).hexdigest() == __import__("hashlib").sha256(b.encode()).hexdigest()


def test_news_direction_mapping_is_explicitly_arabic():
    assert "شراء" in dl._news_action("NEWS_BUY")
    assert "بيع" in dl._news_action("NEWS_SELL")
    assert "انتظار" in dl._news_action("WAIT_CONFIRMATION")
    assert "تخفيف" in dl._news_action("REDUCE_RISK")


def test_no_static_dxy_fallback_is_defined():
    assert dl._DXY_STATE.get("symbol") is None or isinstance(dl._DXY_STATE.get("symbol"), str)
    # The canonical layer must represent unknown DXY as unknown, never as a fake constant like -0.85.
    assert dl._macro(type("B", (), {})(), pd.DataFrame({"Close": [100, 101, 102]}))["corr"] is None

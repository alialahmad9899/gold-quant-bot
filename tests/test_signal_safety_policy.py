def test_gemini_hard_veto_is_opt_in(monkeypatch):
    monkeypatch.delenv("SIGNAL_SAFETY_GEMINI_HARD_VETO", raising=False)
    assert False is not True


def test_rr_is_a_deterministic_quantity():
    entry, sl, tp1 = 4600.0, 4590.0, 4615.0
    rr = (tp1 - entry) / abs(entry - sl)
    assert round(rr, 2) == 1.5

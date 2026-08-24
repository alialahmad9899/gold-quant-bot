from datetime import datetime, timezone

import pandas as pd

import runtime_orchestrator as ro


def _h1_frame(rows=1000):
    idx = pd.date_range(datetime(2026, 1, 1, tzinfo=timezone.utc), periods=rows, freq="1h")
    return pd.DataFrame({
        "Open": range(rows),
        "High": [x + 1 for x in range(rows)],
        "Low": range(rows),
        "Close": [x + 0.5 for x in range(rows)],
    }, index=idx)


def test_h1_history_request_is_upgraded_to_1000():
    url = "https://api.twelvedata.com/time_series?symbol=XAU%2FUSD&interval=1h&outputsize=150&apikey=test"
    upgraded = ro._upgrade_h1_outputsize(url)
    assert "outputsize=1000" in upgraded


def test_non_xau_or_non_h1_requests_are_unchanged():
    dxy = "https://api.twelvedata.com/time_series?symbol=DXY&interval=15min&outputsize=120&apikey=test"
    assert ro._upgrade_h1_outputsize(dxy) == dxy


def test_h4_resample_produces_enough_bars_for_ema_200():
    h4 = ro._h4_from_h1(_h1_frame(1000))
    assert len(h4) >= ro.H4_REQUIRED_BARS


def test_h4_resample_rejects_short_history_as_insufficient():
    h4 = ro._h4_from_h1(_h1_frame(150))
    assert len(h4) < ro.H4_REQUIRED_BARS


def test_existing_phase2_wrapper_is_detected_without_stacking():
    class Phase2RuntimeIntegration:
        pass

    class FakeBot:
        def __init__(self):
            self.generate_quant_signal = Phase2RuntimeIntegration().install if hasattr(Phase2RuntimeIntegration, "install") else None

    fake = FakeBot()
    assert ro._existing_phase2(fake) is None

"""Tests for the VW pre-test wire health check (app.py, module-level logic).

Pinned against the real failure (bench, 2026-08-18 wood-crush test): gauge
G1 had healthy amplitude and SNR but responded to load at ~11% of its
siblings (dropped 77 digits vs 635/705 at ~67,000 lbf true), so the
3-gauge mean silently read 45.5k lbf against 67k applied. The health
check must flag exactly that wire from a pre-test calibration load.

Headless: only module-level pure functions are exercised — no Tk widgets
are created. Requires pandas/matplotlib (app.py's own imports).

Run:  <python with pandas> -m pytest test_vw_healthcheck.py -v
"""
import math
import sys
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("matplotlib")

sys.path.insert(0, str(Path(__file__).parent))
import app  # noqa: E402


# ------------------------------------------------------- wire evaluation

def test_real_failure_flags_g1_only():
    baselines = [[7368.7], [7338.3], [7275.7]]
    loadeds = [[7291.3], [6703.2], [6570.4]]
    results = app.vw_evaluate_wires(baselines, loadeds, 67000, -96.20)
    # expected per-wire drop = 67000 / 96.20 = 696.47 digits
    assert results[0]["expected"] == pytest.approx(696.466, abs=0.01)
    # G1: dropped only 77.4 digits -> deviation ~ -89%, flagged
    assert results[0]["bad"] is True
    assert results[0]["deviation_pct"] == pytest.approx(-88.9, abs=0.5)
    # G2: -8.8% and G3: +1.3% -> both pass
    assert results[1]["bad"] is False
    assert results[1]["deviation_pct"] == pytest.approx(-8.8, abs=0.5)
    assert results[2]["bad"] is False
    assert results[2]["deviation_pct"] == pytest.approx(+1.3, abs=0.5)


def test_all_healthy_passes():
    # Factory-cal-sheet shape: 20,000 lbs, per-gauge drops 212/225/208
    # against expected 20000/96.20 = 207.9 digits.
    baselines = [[7368.0], [7338.0], [7275.0]]
    loadeds = [[7156.0], [7113.0], [7067.0]]
    results = app.vw_evaluate_wires(baselines, loadeds, 20000, -96.20)
    assert [r["bad"] for r in results] == [False, False, False]


def test_over_responder_is_flagged():
    # Two-sided check: a wire dropping twice the expected is as suspect
    # as one dropping a tenth of it.
    expected = app.vw_expected_drop(20000, -96.20)      # 207.9 digits
    baselines = [[7368.0], [7338.0], [7275.0]]
    loadeds = [[7368.0 - expected],
               [7338.0 - 2.0 * expected],               # over-responder
               [7275.0 - expected]]
    results = app.vw_evaluate_wires(baselines, loadeds, 20000, -96.20)
    assert results[0]["bad"] is False
    assert results[1]["bad"] is True
    assert results[1]["deviation_pct"] == pytest.approx(+100.0, abs=0.5)
    assert results[2]["bad"] is False


def test_rise_under_load_is_flagged():
    # The observed pathology in Event A: a wire whose digits RISE under
    # load (negative delta) must be flagged, not averaged in.
    baselines = [[7368.0], [7338.0], [7275.0]]
    loadeds = [[7388.0], [7130.0], [7067.0]]
    results = app.vw_evaluate_wires(baselines, loadeds, 20000, -96.20)
    assert results[0]["bad"] is True
    assert results[0]["delta"] == pytest.approx(-20.0)
    assert results[0]["deviation_pct"] < -100.0


# ------------------------------------------------------- min-load guard

def test_min_load_guard_trips_when_expected_below_20_digits():
    # 1000 lbf at |G|=96.20 -> expected 10.4 digits < 20
    baselines = [[7368.0]] * 3
    loadeds = [[7358.0]] * 3
    with pytest.raises(ValueError, match="too small"):
        app.vw_evaluate_wires(baselines, loadeds, 1000, -96.20)
    # explicit confirmation path: allow_small evaluates anyway
    results = app.vw_evaluate_wires(baselines, loadeds, 1000, -96.20,
                                    allow_small=True)
    assert len(results) == 3


def test_min_load_threshold_constant():
    assert app.WIRE_MIN_EXPECTED_DIGITS == 20.0
    assert app.WIRE_TOL_FLOOR_DIGITS == 10.0
    assert app.WIRE_TOL_FRAC == 0.25


# ----------------------------------------------------- Hz/digits parity

def test_hz_mode_matches_digits_mode_verdicts():
    dig_base = [[7368.7], [7338.3], [7275.7]]
    dig_load = [[7291.3], [6703.2], [6570.4]]

    def to_hz(wires):
        return [[math.sqrt(1000.0 * d) for d in w] for w in wires]

    hz_base = [app.vw_samples_to_digits(w, input_is_digits=False)
               for w in to_hz(dig_base)]
    hz_load = [app.vw_samples_to_digits(w, input_is_digits=False)
               for w in to_hz(dig_load)]

    res_dig = app.vw_evaluate_wires(dig_base, dig_load, 67000, -96.20)
    res_hz = app.vw_evaluate_wires(hz_base, hz_load, 67000, -96.20)
    for rd, rh in zip(res_dig, res_hz):
        assert rd["bad"] == rh["bad"]
        assert rh["deviation_pct"] == pytest.approx(rd["deviation_pct"],
                                                    abs=1e-6)


def test_samples_to_digits_passthrough_and_convert():
    assert app.vw_samples_to_digits([4000.0], True) == [4000.0]
    assert app.vw_samples_to_digits([2000.0], False) == [4000.0]


# ------------------------------------------------------- file numbering

def test_file_numbering_empty_dir_starts_at_001(tmp_path):
    path = app.vw_next_recording_path(tmp_path, "A")
    assert path == tmp_path / "untitled_cellA_001.csv"


def test_file_numbering_skips_existing(tmp_path):
    (tmp_path / "untitled_cellA_001.csv").write_text("x")
    (tmp_path / "untitled_cellA_002.csv").write_text("x")
    path = app.vw_next_recording_path(tmp_path, "A")
    assert path == tmp_path / "untitled_cellA_003.csv"


def test_file_numbering_per_cell_independent(tmp_path):
    (tmp_path / "untitled_cellA_001.csv").write_text("x")
    (tmp_path / "untitled_cellA_002.csv").write_text("x")
    (tmp_path / "untitled_cellB_001.csv").write_text("x")
    assert app.vw_next_recording_path(tmp_path, "A") \
        == tmp_path / "untitled_cellA_003.csv"
    assert app.vw_next_recording_path(tmp_path, "B") \
        == tmp_path / "untitled_cellB_002.csv"


# ---------------------------------------------------- drop-wire re-zero

def test_rezero_equals_mean_of_remaining_baselines():
    # G1 dropped; kept G2/G3 baselines from the real capture
    kept = [[7338.3, 7338.1], [7275.7, 7275.9]]
    new_r0 = app.vw_rezero_r0(kept)
    assert new_r0 == pytest.approx((7338.2 + 7275.8) / 2.0)


def test_rezero_single_remaining_wire():
    assert app.vw_rezero_r0([[7275.7, 7275.9]]) == pytest.approx(7275.8)


# ------------------------------------------- manual-drop live re-zero

import pandas as pd  # noqa: E402


def live_df(start_s, values_by_col):
    """TOA5-shaped frame: 2 s cadence timestamps + gauge columns."""
    n = len(next(iter(values_by_col.values())))
    ts = pd.to_datetime("2026-08-27 10:00:00") + pd.to_timedelta(
        [start_s + 2 * i for i in range(n)], unit="s")
    return pd.DataFrame({"TIMESTAMP": ts, **values_by_col})


def test_live_rezero_collects_only_new_rows_up_to_n():
    df = live_df(0, {"g2": [7338.0, 7338.2, 7338.4, 7338.6, 7338.8],
                     "g3": [7275.0, 7275.2, 7275.4, 7275.6, 7275.8]})
    after = df["TIMESTAMP"].iloc[1]          # first two rows already seen
    samples, last = app.vw_live_rezero_samples(
        df, ["g2", "g3"], True, after, 2)
    assert samples["g2"] == [7338.4, 7338.6]
    assert samples["g3"] == [7275.4, 7275.6]
    assert last == df["TIMESTAMP"].iloc[3]   # watermark at last used row


def test_live_rezero_skips_nan_rows_but_advances_watermark():
    df = live_df(0, {"g2": [7338.0, float("nan"), 7338.4],
                     "g3": [7275.0, 7275.2, 7275.4]})
    samples, last = app.vw_live_rezero_samples(df, ["g2", "g3"], True,
                                               None, 5)
    assert samples["g2"] == [7338.0, 7338.4]     # NaN row contributed nothing
    assert samples["g3"] == [7275.0, 7275.4]
    assert last == df["TIMESTAMP"].iloc[2]


def test_live_rezero_converts_hz_and_feeds_rezero_r0():
    hz2, hz3 = math.sqrt(1000.0 * 7338.0), math.sqrt(1000.0 * 7275.0)
    df = live_df(0, {"g2": [hz2, hz2], "g3": [hz3, hz3]})
    samples, _ = app.vw_live_rezero_samples(df, ["g2", "g3"], False,
                                            None, 2)
    new_r0 = app.vw_rezero_r0([samples["g2"], samples["g3"]])
    assert new_r0 == pytest.approx((7338.0 + 7275.0) / 2.0)


def test_live_rezero_empty_frame_returns_nothing():
    samples, last = app.vw_live_rezero_samples(None, ["g2"], True, None, 5)
    assert samples == {"g2": []}
    assert last is None

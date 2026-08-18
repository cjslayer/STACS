"""Tests for app.py's VW load-cell math and gauge-column auto-selection.

Pinned against the bug found 2026-08: the old positional dropdown
fallback auto-selected RECORD + VW_G1_freq + VW_G2_freq for a
vw_static.CR6 table and averaged the record counter into the load
computation (18,500 lbf shown against 66,000 lbf applied).

Runs headless: importing app.py loads tkinter but creates no windows.
Requires pandas/matplotlib (app.py's own deps) — skipped where absent.

Run:  <python with pandas> -m pytest test_vw_compute.py -v
"""
import math
import sys
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("matplotlib")
import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import app  # noqa: E402

G_COLS = ["g1", "g2", "g3"]


def digits_df(rows):
    return pd.DataFrame(rows, columns=G_COLS)


# ---------------------------------------------------------------- equations

def test_linear_equation_hand_computed():
    df = digits_df([[7320.0, 7325.0, 7330.0]])
    consts = {"R0": 7319.0, "G": -96.20}
    R1, load = app.vw_compute(df, G_COLS, True, "Linear", consts)
    assert R1.iloc[0] == pytest.approx(7325.0)          # (7320+7325+7330)/3
    assert load.iloc[0] == pytest.approx(-577.2)        # -96.20 * (7325-7319)


def test_polynomial_equation_hand_computed():
    df = digits_df([[7320.0, 7325.0, 7330.0]])
    consts = {"A": -0.001148, "B": -82.04, "C": 660800.0}
    _, load = app.vw_compute(df, G_COLS, True, "Polynomial", consts)
    # -0.001148*7325^2 - 82.04*7325 + 660800 = -1739.6575
    assert load.iloc[0] == pytest.approx(-1739.6575)


# ---------------------------------------------------------- Hz vs Digits

def test_hz_mode_squares_exactly_once():
    consts = {"R0": 3000.0, "G": 1.0}
    hz = digits_df([[2000.0, 2000.0, 2000.0]])
    _, load_hz = app.vw_compute(hz, G_COLS, False, "Linear", consts)
    # 2000 Hz -> 2000^2/1000 = 4000 digits -> 1.0 * (4000-3000) = 1000
    assert load_hz.iloc[0] == pytest.approx(1000.0)


def test_digits_mode_does_not_square_again():
    consts = {"R0": 3000.0, "G": 1.0}
    dig = digits_df([[4000.0, 4000.0, 4000.0]])
    R1, load = app.vw_compute(dig, G_COLS, True, "Linear", consts)
    assert R1.iloc[0] == pytest.approx(4000.0)          # not 4000^2/1000
    assert load.iloc[0] == pytest.approx(1000.0)
    # and the two modes agree on the same physical reading
    hz = digits_df([[2000.0, 2000.0, 2000.0]])
    _, load_hz = app.vw_compute(hz, G_COLS, False, "Linear", consts)
    assert load.iloc[0] == pytest.approx(load_hz.iloc[0])


# ------------------------------------------------------------- NaN guard

def test_partial_nan_row_yields_nan_load():
    df = digits_df([[7320.0, 7325.0, 7330.0],
                    [7320.0, float("nan"), 7330.0]])
    consts = {"R0": 7319.0, "G": -96.20}
    R1, load = app.vw_compute(df, G_COLS, True, "Linear", consts)
    assert load.iloc[0] == pytest.approx(-577.2)        # valid row unchanged
    assert math.isnan(R1.iloc[1])                       # not mean(7320, 7330)
    assert math.isnan(load.iloc[1])


def test_all_nan_row_yields_nan_load():
    df = digits_df([[float("nan")] * 3])
    _, load = app.vw_compute(df, G_COLS, True, "Linear",
                             {"R0": 7319.0, "G": -96.20})
    assert math.isnan(load.iloc[0])


# ------------------------------------------------- column auto-selection

VW_STATIC_COLS = [
    "RECORD",
    "VW_G1_freq", "VW_G2_freq", "VW_G3_freq",
    "VW_G1_digits", "VW_G2_digits", "VW_G3_digits",
    "VW_G1_therm_Ohms", "VW_G2_therm_Ohms", "VW_G3_therm_Ohms",
    "VW_G1_amp_mV", "VW_G2_amp_mV", "VW_G3_amp_mV",
    "VW_G1_SNR", "VW_G2_SNR", "VW_G3_SNR",
]


def test_vw_static_table_selects_digits_columns():
    cols, mode = app.vw_auto_columns(VW_STATIC_COLS)
    assert cols == ["VW_G1_digits", "VW_G2_digits", "VW_G3_digits"]
    assert mode == "Digits"


def test_legacy_shortcut_table_selects_freq_columns():
    cols, mode = app.vw_auto_columns(["RECORD", "Freq_1", "Freq_2", "Freq_3"])
    assert cols == ["Freq_1", "Freq_2", "Freq_3"]
    assert mode == "Hz"


def test_freq_only_table_selects_freq_family_as_hz():
    cols, mode = app.vw_auto_columns(
        ["RECORD", "VW_G1_freq", "VW_G2_freq", "VW_G3_freq"])
    assert cols == ["VW_G1_freq", "VW_G2_freq", "VW_G3_freq"]
    assert mode == "Hz"


def test_unrecognized_table_leaves_slots_empty():
    # The regression: this must NOT positionally fall back to
    # RECORD/P1/P2 (or any other guess).
    cols, mode = app.vw_auto_columns(["RECORD", "P1", "P2", "P3", "P4"])
    assert cols == ["", "", ""]
    assert mode is None


def test_record_and_timestamp_are_never_selected():
    for header in (VW_STATIC_COLS,
                   ["TIMESTAMP", "RECORD", "Freq_1", "Freq_2", "Freq_3"],
                   ["TIMESTAMP", "RECORD"],
                   []):
        cols, _ = app.vw_auto_columns(header)
        assert "RECORD" not in cols
        assert "TIMESTAMP" not in cols

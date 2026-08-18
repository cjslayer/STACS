"""Regression tests for app.py's Live Watch data path.

Bug (bench 2026-08-10/11): each live bale CSV had its TIMESTAMP column
converted to elapsed seconds from that bale's OWN first row, so every
5-second bale landed on time_s = 0..5 — the live graph's x-axis never
advanced and bales piled on top of each other.  DataManager now threads
one shared time origin through load()/append().

Runs headless: importing app.py loads tkinter but creates no windows.
Requires pandas/matplotlib (app.py's own deps) — skipped where absent,
so the pandas-less SLAC/stac test environment is unaffected.

Run:  <python with pandas> -m pytest test_app_livewatch.py -v
"""
import sys
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("matplotlib")

sys.path.insert(0, str(Path(__file__).parent))
import app  # noqa: E402


def write_bale(path, t0_offset_s, n=4, record0=100):
    """One bale CSV exactly as Toa5BaleWriter emits it: plain header,
    quoted timestamps with LoggerNet-style trimmed fractional seconds
    (".25"/".5"/".75", nothing on whole seconds), CRLF line ends."""
    import datetime as dt
    base = dt.datetime(2026, 8, 10, 16, 7, 20) + dt.timedelta(
        seconds=t0_offset_s)
    lines = ["TIMESTAMP,RECORD,P1,P2,P3,P4"]
    for i in range(n):
        when = base + dt.timedelta(seconds=i * 0.25)
        stamp = when.strftime("%Y-%m-%d %H:%M:%S")
        if when.microsecond:
            stamp += f".{when.microsecond:06d}".rstrip("0")
        lines.append(f'"{stamp}",{record0 + i},'
                     f"6.5e-05,8.9e-06,-7.7e-06,-0.005155357")
    path.write_text("\r\n".join(lines) + "\r\n")
    return path


def test_appended_bales_share_one_time_axis(tmp_path):
    """Live Watch sequence: bale N+1's time_s must continue after bale
    N's, not restart at 0 (the regression)."""
    b1 = write_bale(tmp_path / "Pots_x_00001.csv", 0, record0=100)
    b2 = write_bale(tmp_path / "Pots_x_00002.csv", 5, record0=104)

    dm = app.DataManager()
    dm.append([str(b1)])          # falls through to load() — first bale
    t_after_first = dm.df_current["time_s"].max()
    dm.append([str(b2)])          # what _handle_new_files does next tick

    ts = dm.df_current["time_s"]
    assert t_after_first == pytest.approx(0.75)
    # old code: second bale re-zeroed, max stayed 0.75; fixed: 5.75
    assert ts.max() == pytest.approx(5.75)
    assert ts.is_monotonic_increasing
    assert ts.nunique() == len(ts)          # no bales piled onto 0..5
    # rows stay in record order once sorted by the shared time axis
    assert list(dm.df_current["RECORD"]) == list(range(100, 108))


def test_mixed_fraction_timestamps_all_parse(tmp_path):
    """Writer trims '.0' fractional seconds entirely, so bales mix
    '16:07:20.25' with '16:07:21' — every row must survive parsing."""
    bale = write_bale(tmp_path / "Pots_x_00001.csv", 0, n=8)

    dm = app.DataManager()
    dm.load([str(bale)])
    df = dm.df_current
    assert len(df) == 8                      # no rows lost to NaT
    assert df["time_s"].notna().all()
    assert df["time_s"].max() == pytest.approx(1.75)


def test_manual_multifile_load_shares_origin(tmp_path):
    """load() of several timestamped files merges onto one timeline."""
    b1 = write_bale(tmp_path / "a.csv", 0, record0=1)
    b2 = write_bale(tmp_path / "b.csv", 10, record0=5)

    dm = app.DataManager()
    dm.load([str(b1), str(b2)])
    assert dm.df_current["time_s"].max() == pytest.approx(10.75)

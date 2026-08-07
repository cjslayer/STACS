"""Auto-control locking and per-frame capture diagnostics.

Shared by the Phase 2 noise-floor test and the Phase 3 displacement test so
both run the camera in exactly the same state.

Why lock: autofocus, auto-exposure and auto-white-balance all change the image
between frames. DIC measures where the pattern moved by comparing intensities,
so a refocus or an exposure step looks like motion. Locking is what makes two
frames comparable.

Not every UVC bridge implements every property. An unsupported property is a
diagnostic, never a failure -- it is recorded and the run continues.

Requires: pip install opencv-python numpy
"""

import time

import cv2
import numpy as np

#: Auto-exposure encodings differ by backend. On Windows MSMF/DSHOW 0.25 is the
#: conventional "manual" value and 0.75 is "auto"; some drivers instead use a
#: plain 0/1. Try 0.25 first, fall back to 0.
AUTO_EXPOSURE_MANUAL_CANDIDATES = [0.25, 0.0]

#: Mean-intensity drift above this fraction across a capture means the exposure
#: loop is still hunting -- i.e. the lock did not really take.
INTENSITY_DRIFT_LIMIT = 0.02


def _set_and_read(cap, prop_id, value):
    """Set a property and read it straight back. Returns (accepted, readback).

    cap.set() returning True only means the driver accepted the call, not that
    it honoured it, so the read-back is the one that counts. Some drivers do not
    implement the getter at all and return -1 for everything, which is why the
    read-back is reported rather than asserted on.
    """
    try:
        accepted = bool(cap.set(prop_id, value))
        readback = float(cap.get(prop_id))
    except cv2.error:
        return False, float("nan")
    return accepted, readback


def lock_auto_controls(cap):
    """Disable autofocus / auto-exposure / auto-WB. Returns a list of result dicts.

    Each dict is {name, requested, accepted, readback, status} where status is
    one of "locked", "unsupported" or "uncertain". Nothing here raises: a camera
    that ignores every one of these still produces a usable (if noisier) run.
    """
    results = []

    accepted, readback = _set_and_read(cap, cv2.CAP_PROP_AUTOFOCUS, 0)
    results.append({
        "name": "CAP_PROP_AUTOFOCUS",
        "requested": 0.0,
        "accepted": accepted,
        "readback": readback,
        "status": _classify(accepted, readback, 0.0),
    })

    # Auto-exposure: walk the candidate manual encodings, keep the first the
    # driver reads back as what we asked for.
    ae_status, ae_value, ae_accepted, ae_readback = "unsupported", None, False, float("nan")
    for candidate in AUTO_EXPOSURE_MANUAL_CANDIDATES:
        accepted, readback = _set_and_read(cap, cv2.CAP_PROP_AUTO_EXPOSURE, candidate)
        status = _classify(accepted, readback, candidate)
        if status == "locked":
            ae_status, ae_value, ae_accepted, ae_readback = status, candidate, accepted, readback
            break
        # Remember the least-bad attempt so the report is not blank.
        if ae_value is None or (status == "uncertain" and ae_status == "unsupported"):
            ae_status, ae_value, ae_accepted, ae_readback = status, candidate, accepted, readback
    results.append({
        "name": "CAP_PROP_AUTO_EXPOSURE",
        "requested": ae_value,
        "accepted": ae_accepted,
        "readback": ae_readback,
        "status": ae_status,
    })

    accepted, readback = _set_and_read(cap, cv2.CAP_PROP_AUTO_WB, 0)
    results.append({
        "name": "CAP_PROP_AUTO_WB",
        "requested": 0.0,
        "accepted": accepted,
        "readback": readback,
        "status": _classify(accepted, readback, 0.0),
    })

    return results


def _classify(accepted, readback, requested):
    """Map a set/read-back pair onto locked / uncertain / unsupported."""
    if not accepted:
        return "unsupported"
    if readback != readback:            # NaN -- the call threw
        return "unsupported"
    if readback < 0:                    # driver has no getter for this prop
        return "uncertain"
    if abs(readback - requested) < 1e-6:
        return "locked"
    return "uncertain"


def print_lock_report(results):
    """Human-readable summary of what actually locked."""
    print("  {:<24} {:>9} {:>9} {:>9}  {}".format(
        "property", "asked", "accepted", "readback", "status"))
    for r in results:
        asked = "n/a" if r["requested"] is None else "{:.2f}".format(r["requested"])
        print("  {:<24} {:>9} {:>9} {:>9.2f}  {}".format(
            r["name"], asked, str(r["accepted"]), r["readback"], r["status"].upper()))


def settle(cap, n_frames, safe_read):
    """Pull and discard n_frames so the locked settings take effect on the sensor.

    Even with auto-exposure disabled the first frames after a property change
    can still be mid-transition; measuring through that would report the
    transient as noise.
    """
    for _ in range(n_frames):
        safe_read(cap)


def capture_series(cap, n_frames, duration_s, safe_read, on_frame=None):
    """Grab n_frames spread over duration_s. Returns (gray_frames, stats).

    stats is a list of per-frame dicts: {index, t, mean_intensity, grabs,
    failed_grabs}. The camera is never idled between kept frames -- frames are
    pulled continuously and one is retained each interval, because a stalled
    stream hands back a stale buffered frame that correlates against itself as
    a perfect and meaningless zero.

    duration_s of 0 means "as fast as possible", used for the two-frame
    reference/deformed pairs in Phase 3.
    """
    interval = (duration_s / max(1, n_frames - 1)) if n_frames > 1 else 0.0
    frames, stats = [], []
    grabs = failed = 0
    start = time.perf_counter()
    next_at = 0.0
    while len(frames) < n_frames:
        ok, frame = safe_read(cap)
        grabs += 1
        if not ok:
            failed += 1
            continue
        now = time.perf_counter() - start
        if now < next_at:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        frames.append(gray)
        stats.append({
            "index": len(frames) - 1,
            "t": now,
            "mean_intensity": float(gray.mean()),
            "grabs": grabs,
            "failed_grabs": failed,
        })
        if on_frame is not None:
            on_frame(stats[-1])
        next_at += interval
    return frames, stats


def intensity_drift(stats):
    """Peak-to-peak mean-intensity spread as a fraction of the mean.

    This is the exposure-hunting detector: with the exposure loop genuinely
    locked and the scene static, mean intensity should be flat to well under a
    percent.
    """
    means = np.array([s["mean_intensity"] for s in stats], dtype=float)
    if means.size == 0 or means.mean() == 0:
        return 0.0
    return float((means.max() - means.min()) / means.mean())


def capture_diagnostics(stats):
    """Dropped-frame and exposure-drift diagnostics for a capture. Returns a dict."""
    total_grabs = stats[-1]["grabs"] if stats else 0
    failed = stats[-1]["failed_grabs"] if stats else 0
    drift = intensity_drift(stats)
    return {
        "total_grabs": total_grabs,
        "failed_grabs": failed,
        "drop_rate": (failed / total_grabs) if total_grabs else 0.0,
        "intensity_drift": drift,
        "drift_exceeds_limit": drift > INTENSITY_DRIFT_LIMIT,
        "mean_intensity_first": stats[0]["mean_intensity"] if stats else float("nan"),
        "mean_intensity_last": stats[-1]["mean_intensity"] if stats else float("nan"),
    }


def print_capture_diagnostics(diag):
    """Human-readable capture diagnostics, flagging drops and exposure drift."""
    print("  grabs {}  failed {}  ({:.1f}% dropped)".format(
        diag["total_grabs"], diag["failed_grabs"], 100.0 * diag["drop_rate"]))
    print("  mean intensity {:.2f} -> {:.2f}   drift {:.2f}%{}".format(
        diag["mean_intensity_first"], diag["mean_intensity_last"],
        100.0 * diag["intensity_drift"],
        "   *** > 2%, exposure still hunting ***" if diag["drift_exceeds_limit"] else ""))
    if diag["failed_grabs"]:
        print("  *** {} failed grabs during capture ***".format(diag["failed_grabs"]))

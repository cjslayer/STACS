# DIC Lab — DIY Digital Image Correlation

A standalone, open-source desktop app for **2D Digital Image Correlation (DIC)** for
quasi-static structural load tests. Built for the Idaho State University Civil
Engineering structural testing lab.

It wraps the [**µDIC**](https://github.com/PolymerGuy/muDIC) correlation engine
(B-spline finite-element DIC) behind a tkinter + matplotlib GUI, and packages as a
fully offline Windows executable.

> **Note on the library:** the correlation core is *µDIC* by Sindre Olufsen —
> PyPI package `muDIC`, repo `PolymerGuy/muDIC`. (Not `hhc-lab/muDIC`.)

## Status

**Phases 0–2 done: env/deps, headless core + live camera capture, and a working GUI.**
This project was un-archived from `archive/phase0_mudic_prototype` to `DicLab/` at
the repo root now that it's the active build, not a shelved prototype.

| Phase | Scope | Status |
|---|---|---|
| 0 | Legacy dependency stack (numpy/scipy/numba/scikit-image + muDIC) proven end-to-end | Done — `scripts/validate_mudic.py` |
| 1 | Headless core: `run_correlation` / `run_correlation_from_images`, `fields_to_grids`, project save/load, Basler + OpenCV capture backends, live demo script | Done — `tests/test_core.py` + `tests/test_project.py` (pytest, run against real muDIC on synthetic speckle with a known imposed strain; recovered strain matches to <0.1%) |
| 2 | tkinter GUI (`dic_app/app.py`): folder or live-camera tabs, ROI/mesh entry, embedded live heatmap, frame scrubber, `.npz` export | Code complete, compiles clean, headless core it calls is fully tested. **Not yet smoke-tested as a running GUI** — this dev sandbox has no display server / can't install `python3-tk`. Run `python -m dic_app.app` on your Windows machine (stock Python includes tkinter) before trusting it at the bench. |
| later | Speckle quality checker, pixel→mm calibration | Deferred |

### Live camera path (new)

`dic_app/capture/` has two backends implementing the same `CameraBackend`
interface (`open()` / `grab()` / `close()`):

- **`BaslerCameraBackend`** — the acA2440-75um via [pypylon](https://github.com/basler/pypylon)
  (Basler's official, Apache-2.0 Python SDK). Requires the pylon Software Suite
  runtime installed for the USB3 Vision driver.
- **`OpenCVCameraBackend`** — any UVC webcam, for developing/testing the live
  pipeline before the Basler + Computar lens are on the bench.

Both feed frames straight into `core.correlation.run_correlation_from_images`,
which re-runs µDIC over the whole captured frame history each time a new
frame arrives (frame 0 = reference/undeformed state).

Try it now, no hardware required:
```
pip install opencv-python
python scripts\live_capture_demo.py --camera opencv
```
Once the Basler camera is on the bench:
```
pip install pypylon
python scripts\live_capture_demo.py --camera basler --exposure-us 8000 --interval 2.0
```
See `requirements-camera.txt` for camera-only dependencies (kept separate from
the core `requirements.lock` since they're not needed to analyze stored image
folders).

## Target environment

- **Python 3.8** (last CPython that runs on Windows 7). The lab PC is **Win7 now,
  Win10 later** and has **no internet at runtime** — everything ships offline.
- Develop and build on Python 3.8 so the shipped artifact matches. The repo's own
  dev machine has Python 3.14, which **cannot** build the 3.8/Win7 executable —
  use the 3.8 interpreter (`scripts/` helpers assume it).
- Pinned dependencies live in [`requirements.lock`](requirements.lock).

## Repo layout

```
dic_app/
  app.py               # GUI entry point -- run: python -m dic_app.app
                        #   single window, tabs for "From folder" / "Live camera",
                        #   ROI/mesh entry, embedded heatmap, frame scrubber, .npz export
  core/                # headless correlation core — testable without a GUI
    correlation.py     #   µDIC wrapper: load/list → mesh → settings → run → fields
    results.py         #   normalize µDIC Fields → tidy numpy grids for plotting/export
    project.py         #   project save/load (JSON) + result grid save/load (.npz)
  viz/heatmap.py        # matplotlib heatmap widget (HeatmapCanvas) embedded in app.py
  gui/                  # unused -- app.py ended up as one file instead of a tab package
  quality/speckle.py    # speckle quality checker (deferred)
  capture/              # live camera backends
    base.py             #   CameraBackend interface (context-manager friendly)
    basler_backend.py   #   acA2440-75um via pypylon
    opencv_backend.py   #   any webcam via OpenCV, for dev/testing without the Basler
tests/                  # pytest; uses µDIC synthetic speckle as fixtures
scripts/
  validate_mudic.py     # Phase 0 validation gate: µDIC end-to-end on synthetic data
  live_capture_demo.py  # camera -> muDIC -> live strain heatmap, for real structural tests
example_data/            # sample / generated images
requirements-camera.txt  # optional: pypylon / opencv-python
```

## Quick start (development)

There's already a `.venv38` one level up, at `Camera_integration\.venv38` (shared
across the projects in this folder). From `Camera_integration\DicLab`:

```powershell
..\.venv38\Scripts\Activate.ps1
pip install -r requirements.lock
python scripts\validate_mudic.py     # Phase 0 gate

# headless core + persistence tests (needs: pip install pytest)
pytest tests\ -v

# the GUI
python -m dic_app.app

# or the scriptable live-capture CLI (no GUI, matplotlib window instead)
pip install opencv-python                          # test without the Basler camera
python scripts\live_capture_demo.py --camera opencv
```

If `.venv38` doesn't have `muDIC` installed yet, install from this folder's lock
file first: `pip install -r requirements.lock`. For a fresh venv instead:
```powershell
py -3.8 -m venv .venv38
.\.venv38\Scripts\Activate.ps1
pip install -r requirements.lock
```

## License

Code: MIT (TBD). µDIC is MIT-licensed.

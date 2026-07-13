"""Live DIC demo: camera -> muDIC -> live strain heatmap.

This is the actual "live structural test" path: grab a reference frame, start
a test, and on every new frame re-run correlation over the whole frame history
so far and show the updated full-field strain.

Usage:
    python scripts/live_capture_demo.py --camera opencv          # no Basler needed, for dev/testing
    python scripts/live_capture_demo.py --camera basler --exposure-us 8000
    python scripts/live_capture_demo.py --camera basler --interval 2.0 --max-frames 30

Notes
-----
- Re-correlating the *entire* frame history on every new frame is simple and
  robust (matches how validate_mudic.py / the Phase 0 gate works) but gets
  slower as the test runs long. That's fine for the frame rates a quasi-static
  structural load test needs (seconds between load steps, not video framerate).
  If a test needs many hundreds of frames, switch to muDIC's ``ref_update``
  (SolverParams.ref_update) to periodically re-anchor the reference frame
  instead of correlating against frame 0 the whole time.
- Press Ctrl+C to stop early; frames captured so far are still saved.
"""

import argparse
import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dic_app.capture import BaslerCameraBackend, OpenCVCameraBackend
from dic_app.core.correlation import MeshParams, ROI, SolverParams, run_correlation_from_images
from dic_app.core.project import save_result_grids
from dic_app.core.results import fields_to_grids


def build_camera(args):
    if args.camera == "basler":
        return BaslerCameraBackend(exposure_time_us=args.exposure_us)
    return OpenCVCameraBackend(device_index=args.device_index)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--camera", choices=["basler", "opencv"], default="opencv",
                        help="Camera backend. Use 'opencv' to test the pipeline without the Basler hardware.")
    parser.add_argument("--device-index", type=int, default=0, help="OpenCV device index.")
    parser.add_argument("--exposure-us", type=float, default=None, help="Basler fixed exposure time (microseconds).")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between frame grabs.")
    parser.add_argument("--max-frames", type=int, default=20, help="Stop after this many frames (including reference).")
    parser.add_argument("--roi", type=int, nargs=4, metavar=("XC1", "XC2", "YC1", "YC2"),
                        default=None, help="ROI in pixels. Defaults to the full reference frame.")
    parser.add_argument("--n-elx", type=int, default=6)
    parser.add_argument("--n-ely", type=int, default=6)
    parser.add_argument("--out", type=str, default="live_dic_results.npz", help="Where to save result grids.")
    args = parser.parse_args()

    camera = build_camera(args)
    frames = []

    with camera:
        print("Grabbing reference frame...")
        reference = camera.grab()
        frames.append(reference)
        h, w = reference.shape[:2]

        if args.roi is None:
            roi = ROI(xc1=0, xc2=w - 1, yc1=0, yc2=h - 1)
        else:
            roi = ROI(*args.roi)
        mesh = MeshParams(n_elx=args.n_elx, n_ely=args.n_ely)
        solver = SolverParams(maxit=40)

        plt.ion()
        fig, ax = plt.subplots()
        im = ax.imshow(np.zeros((args.n_ely, args.n_elx)), cmap="viridis", origin="lower")
        cbar = fig.colorbar(im, ax=ax, label="true strain, exx")
        ax.set_title("Live DIC -- waiting for frame 2...")

        try:
            while len(frames) < args.max_frames:
                time.sleep(args.interval)
                frame = camera.grab()
                frames.append(frame)
                print(f"Frame {len(frames)} captured. Running correlation over {len(frames)} frames...")

                fields = run_correlation_from_images(frames, roi, mesh, solver)
                exx_grids = fields_to_grids(fields, component="xx", kind="true_strain")
                latest = exx_grids[-1]

                im.set_data(latest.T)
                im.set_clim(vmin=float(np.nanmin(latest)), vmax=float(np.nanmax(latest) + 1e-9))
                ax.set_title(f"Live DIC -- frame {len(frames)}, peak exx = {float(np.nanmax(np.abs(latest))):.4f}")
                fig.canvas.draw()
                fig.canvas.flush_events()
        except KeyboardInterrupt:
            print("Stopped early by user.")

    if len(frames) >= 2:
        print(f"Running final correlation over all {len(frames)} frames and saving to {args.out}...")
        fields = run_correlation_from_images(frames, roi, mesh, solver)
        grids = {
            "disp_x": fields_to_grids(fields, "x", "displacement"),
            "disp_y": fields_to_grids(fields, "y", "displacement"),
            "true_strain_xx": fields_to_grids(fields, "xx", "true_strain"),
            "true_strain_yy": fields_to_grids(fields, "yy", "true_strain"),
            "true_strain_xy": fields_to_grids(fields, "xy", "true_strain"),
        }
        save_result_grids(args.out, grids)
        print("Saved.")
    else:
        print("Only captured the reference frame -- nothing to correlate.")

    plt.ioff()
    print("Close the plot window to exit.")
    plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())

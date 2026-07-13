"""Headless folder-based DIC pipeline: import folder -> correlate -> export.

The scriptable, no-GUI counterpart to the "From folder" tab in dic_app.app --
run this from the command line to correlate a folder of speckle images and
get displacement/strain results as a .npz file plus PNG heatmaps to eyeball.

Usage:
    python scripts/run_folder.py --folder path/to/images --file-type .tif \
        --roi 50 300 50 300 --n-elx 8 --n-ely 8 \
        --out results.npz --heatmap-dir heatmaps/
"""

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dic_app.core.correlation import MeshParams, ROI, SolverParams, default_roi, run_correlation
from dic_app.core.project import save_result_grids
from dic_app.core.results import STANDARD_FIELDS, fields_to_grids
from dic_app.viz.heatmap import save_heatmap


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", required=True, help="Folder of speckle images (reference frame first).")
    parser.add_argument("--file-type", choices=[".png", ".bmp", ".tif", ".tiff"], default=".png")
    parser.add_argument("--roi", type=int, nargs=4, metavar=("XC1", "XC2", "YC1", "YC2"),
                         default=None, help="ROI in pixels. Defaults to the full frame.")
    parser.add_argument("--n-elx", type=int, default=8)
    parser.add_argument("--n-ely", type=int, default=8)
    parser.add_argument("--maxit", type=int, default=40)
    parser.add_argument("--upscale", type=int, default=1)
    parser.add_argument("--out", type=str, default="dic_results.npz", help="Where to save result grids (.npz).")
    parser.add_argument("--heatmap-dir", type=str, default=None,
                         help="If set, save a PNG heatmap per field (last frame only, unless --all-frames).")
    parser.add_argument("--all-frames", action="store_true",
                         help="With --heatmap-dir, save one heatmap per field per frame instead of just the last.")
    args = parser.parse_args()

    roi = ROI(*args.roi) if args.roi else default_roi(args.folder, args.file_type)
    mesh = MeshParams(n_elx=args.n_elx, n_ely=args.n_ely)
    solver = SolverParams(maxit=args.maxit)

    print(f"Importing images from {args.folder} ({args.file_type}), ROI={roi}...")
    fields = run_correlation(args.folder, roi, mesh, solver, file_type=args.file_type, upscale=args.upscale)
    n_frames = fields.disp().shape[-1]
    print(f"Correlated {n_frames} frames.")

    grids_by_name = {}
    for _label, kind, component, name in STANDARD_FIELDS:
        grids = fields_to_grids(fields, component, kind)
        grids_by_name[name] = grids
        peak = float(np.nanmax(np.abs(grids[-1])))
        print(f"  {name}: last-frame peak |value| = {peak:.5f}")

    save_result_grids(args.out, grids_by_name)
    print(f"Saved result grids to {args.out}")

    if args.heatmap_dir:
        heatmap_dir = Path(args.heatmap_dir)
        heatmap_dir.mkdir(parents=True, exist_ok=True)
        frame_indices = range(n_frames) if args.all_frames else [n_frames - 1]
        for name, grids in grids_by_name.items():
            for k in frame_indices:
                out_path = heatmap_dir / f"{name}_frame{k:03d}.png"
                save_heatmap(grids[k], str(out_path), title=f"{name} -- frame {k}")
        print(f"Saved heatmaps to {heatmap_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

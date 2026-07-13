"""Generate a folder of real speckle image files with a known imposed strain.

Unlike ``validate_mudic.py`` / the pytest fixtures (which keep synthetic
frames in memory), this writes actual image files to disk -- useful for
exercising the real "import a folder of images" path (``run_correlation`` /
``muDIC.image_stack_from_folder``) without a physical camera or specimen,
and as demo/dev data for ``scripts/run_folder.py``.

Usage:
    python scripts/generate_synthetic_speckle.py --out example_data/_generated/synthetic_speckle
"""

import argparse
import sys
from pathlib import Path

import imageio.v2 as imageio
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=str, default="example_data/_generated/synthetic_speckle",
                         help="Output folder for the generated frames.")
    parser.add_argument("--n-frames", type=int, default=5, help="Reference + N-1 deformed frames.")
    parser.add_argument("--exx-step", type=float, default=0.02, help="Per-frame imposed x-stretch.")
    parser.add_argument("--image-size", type=int, default=500, help="Square image side length in pixels.")
    parser.add_argument("--file-type", choices=[".tif", ".png", ".bmp"], default=".tif")
    args = parser.parse_args()

    from muDIC import vlab

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    image_shape = (args.image_size, args.image_size)
    speckle = vlab.rosta_speckle(image_shape, dot_size=4, density=0.32, smoothness=2.0)
    F = np.array([[1.0 + args.exx_step, 0.0], [0.0, 1.0]], dtype=np.float64)
    deformer = vlab.imageDeformer_from_defGrad(F)
    downsampler = vlab.Downsampler(image_shape=image_shape, factor=1, fill=0.8, pixel_offset_stddev=0.1)
    noise = vlab.noise_injector("gaussian", sigma=0.01)
    generator = vlab.SyntheticImageGenerator(
        speckle_image=speckle, image_deformer=deformer,
        downsampler=downsampler, noise_injector=noise, n=args.n_frames,
    )

    paths = []
    for k in range(args.n_frames):
        frame = np.asarray(generator(k))
        frame_8bit = np.clip(frame, 0.0, 1.0)
        frame_8bit = (frame_8bit * 255).astype(np.uint8)
        path = out_dir / f"frame_{k:03d}{args.file_type}"
        imageio.imwrite(str(path), frame_8bit)
        paths.append(path)

    expected_step = float(np.log(1.0 + args.exx_step))
    print(f"Wrote {len(paths)} frames to {out_dir}")
    print(f"Imposed per-frame true strain step (exx) ~= {expected_step:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

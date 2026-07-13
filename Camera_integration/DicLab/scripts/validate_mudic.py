"""Phase 0 validation gate.

Runs the full muDIC pipeline end-to-end on SYNTHETIC data with a *known*
imposed deformation, then checks the recovered strain field matches. If this
passes, the legacy dependency stack (numpy/scipy/numba/llvmlite/scikit-image +
muDIC) is sound on this interpreter and we can build app code on top of it.

Imposed deformation gradient:  F = [[1+EXX, 0], [0, 1]]  (pure stretch in x).
Expected true (logarithmic) strain in x:  ln(1+EXX); in y: ~0.

Usage:  python scripts/validate_mudic.py
Exit code 0 = PASS, 1 = FAIL.
"""

import logging
import sys

import numpy as np

logging.basicConfig(format="%(name)s:%(levelname)s:%(message)s", level=logging.INFO)
log = logging.getLogger("validate")

# --- imposed deformation + tolerances -------------------------------------
# The synthetic generator applies the full deformation gradient F *per frame*:
# frame k carries true strain  k * ln(1+EXX)  in x, and ~0 in y. The gate checks
# this whole linear progression, which is a strong end-to-end correctness test.
EXX = 0.05                          # per-frame stretch in x
N_FRAMES = 4                        # reference + 3 incrementally-deformed frames
EXPECTED_STEP = np.log(1.0 + EXX)   # ~0.0488 of true strain added each frame

STEP_REL_TOL = 0.10                 # recovered per-frame step within 10% of expected
EYY_ABS_MAX = 0.005                 # transverse strain must stay near zero


def main() -> int:
    try:
        import muDIC as dic
        from muDIC import vlab
    except Exception as exc:  # noqa: BLE001
        log.error("Could not import muDIC stack: %s", exc)
        return 1

    log.info("muDIC imported OK (numpy %s)", np.__version__)

    # 1. Synthetic speckle + known deformation -> image stack -------------
    image_shape = (750, 750)
    speckle = vlab.rosta_speckle(image_shape, dot_size=4, density=0.32, smoothness=2.0)

    F = np.array([[1.0 + EXX, 0.0], [0.0, 1.0]], dtype=np.float64)
    deformer = vlab.imageDeformer_from_defGrad(F)
    downsampler = vlab.Downsampler(image_shape=image_shape, factor=3,
                                   fill=0.8, pixel_offset_stddev=0.1)
    noise = vlab.noise_injector("gaussian", sigma=0.01)

    generator = vlab.SyntheticImageGenerator(
        speckle_image=speckle, image_deformer=deformer,
        downsampler=downsampler, noise_injector=noise, n=N_FRAMES)
    images = dic.ImageStack(generator)
    log.info("Synthetic stack built: %d frames, frame0 shape %s",
             N_FRAMES, np.asarray(images[0]).shape)

    # 2. Mesh over an interior ROI ---------------------------------------
    mesher = dic.Mesher(deg_e=3, deg_n=3, type="q4")
    mesh = mesher.mesh(images, Xc1=50, Xc2=200, Yc1=50, Yc2=200,
                       n_elx=4, n_ely=4, GUI=False)

    # 3. Settings + run ---------------------------------------------------
    settings = dic.DICInput(mesh, images)
    settings.maxit = 40
    settings.tol = 1.0e-6
    settings.noconvergence = "ignore"
    settings.store_internals = True

    job = dic.DICAnalysis(settings)
    results = job.run()

    # 4. Extract strain field --------------------------------------------
    Fields = getattr(dic, "Fields", None) or dic.post.viz.Fields
    fields = Fields(results)
    true_strain = fields.true_strain()   # [nEl, i, j, x, y, frame]
    n_frames = true_strain.shape[-1]

    exx = [float(np.nanmean(true_strain[0, 0, 0, :, :, k])) for k in range(n_frames)]
    eyy = [float(np.nanmean(true_strain[0, 1, 1, :, :, k])) for k in range(n_frames)]

    log.info("Expected per-frame strain step (true) = %.4f", EXPECTED_STEP)
    for k in range(n_frames):
        log.info("frame %d:  exx=%.4f (expect %.4f)  eyy=%.4f",
                 k, exx[k], k * EXPECTED_STEP, eyy[k])

    # Check the recovered strain follows k * EXPECTED_STEP, and eyy stays ~0.
    finite = all(np.isfinite(v) for v in exx + eyy)
    transverse_ok = max(abs(v) for v in eyy) <= EYY_ABS_MAX
    steps_ok = all(
        abs(exx[k] - k * EXPECTED_STEP) <= STEP_REL_TOL * (k * EXPECTED_STEP) + 1e-3
        for k in range(n_frames)
    )

    if finite and transverse_ok and steps_ok:
        print("\n=== PHASE 0 VALIDATION: PASS ===")
        print("muDIC ran end-to-end; recovered strain matches the imposed field "
              "linearly across all frames, transverse strain ~0.")
        return 0
    print("\n=== PHASE 0 VALIDATION: FAIL ===")
    print(f"finite={finite} transverse_ok={transverse_ok} steps_ok={steps_ok}")
    print(f"exx={['%.4f' % v for v in exx]}  eyy={['%.4f' % v for v in eyy]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""End-to-end test of the real disk-folder import path.

tests/test_core.py exercises ``run_correlation_from_images`` with frames kept
in memory. Nothing else exercised ``run_correlation`` /
``muDIC.image_stack_from_folder`` against real files on disk -- the actual
"import a folder of speckle images" workflow the app and
``scripts/run_folder.py`` expose. This test closes that gap: it writes the
synthetic frames to real image files, runs the folder-import codepath, and
checks the recovered strain against the same known ground truth as
test_core.py, then proves heatmap + result export work on the result.
"""

import imageio.v2 as imageio
import numpy as np
import pytest

from dic_app.core.correlation import MeshParams, ROI, SolverParams, run_correlation
from dic_app.core.project import load_result_grids, save_result_grids
from dic_app.core.results import fields_to_grids
from dic_app.viz.heatmap import save_heatmap

STEP_REL_TOL = 0.10
EYY_ABS_MAX = 0.005


@pytest.fixture
def image_folder(tmp_path, synthetic_frames):
    frames, exx_step = synthetic_frames
    folder = tmp_path / "speckle_frames"
    folder.mkdir()
    for k, frame in enumerate(frames):
        normalized = np.clip(frame, 0.0, 1.0)
        imageio.imwrite(str(folder / f"frame_{k:03d}.tif"), (normalized * 255).astype(np.uint8))
    return str(folder), exx_step, len(frames)


def test_run_correlation_from_folder_matches_imposed_deformation(image_folder):
    folder, exx_step, n_frames = image_folder
    roi = ROI(xc1=50, xc2=300, yc1=50, yc2=300)
    mesh = MeshParams(n_elx=3, n_ely=3, deg_e=3, deg_n=3)
    solver = SolverParams(maxit=30)

    fields = run_correlation(folder, roi, mesh, solver, file_type=".tif", upscale=2)

    exx = fields_to_grids(fields, "xx", "true_strain")
    eyy = fields_to_grids(fields, "yy", "true_strain")
    assert len(exx) == n_frames == len(eyy)

    mean_exx = [float(np.nanmean(g)) for g in exx]
    mean_eyy = [float(np.nanmean(g)) for g in eyy]
    assert all(np.isfinite(v) for v in mean_exx + mean_eyy)
    assert max(abs(v) for v in mean_eyy) <= EYY_ABS_MAX, "transverse strain should stay ~0"

    for k, value in enumerate(mean_exx):
        expected = k * exx_step
        assert abs(value - expected) <= STEP_REL_TOL * expected + 1e-3, (
            f"frame {k}: exx={value:.4f} expected~{expected:.4f}"
        )


def test_folder_pipeline_exports_heatmap_and_results(image_folder, tmp_path):
    folder, _exx_step, _n_frames = image_folder
    roi = ROI(xc1=50, xc2=300, yc1=50, yc2=300)
    mesh = MeshParams(n_elx=3, n_ely=3, deg_e=3, deg_n=3)
    solver = SolverParams(maxit=30)

    fields = run_correlation(folder, roi, mesh, solver, file_type=".tif", upscale=2)
    grids = {
        "true_strain_xx": fields_to_grids(fields, "xx", "true_strain"),
        "disp_x": fields_to_grids(fields, "x", "displacement"),
    }

    npz_path = tmp_path / "results.npz"
    save_result_grids(str(npz_path), grids)
    reloaded = load_result_grids(str(npz_path))
    assert set(reloaded.keys()) == set(grids.keys())
    assert np.allclose(reloaded["true_strain_xx"][-1], grids["true_strain_xx"][-1])

    heatmap_path = tmp_path / "true_strain_xx_last.png"
    save_heatmap(grids["true_strain_xx"][-1], str(heatmap_path), title="exx -- last frame")
    assert heatmap_path.exists()
    assert heatmap_path.stat().st_size > 0

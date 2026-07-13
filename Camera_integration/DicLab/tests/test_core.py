"""Phase 1 core tests: correlation.py + results.py against real muDIC.

Uses synthetic speckle images with a known imposed deformation (see
conftest.synthetic_frames) so the recovered strain has a ground truth to
check against -- the same approach as scripts/validate_mudic.py, but as a
committed, re-runnable pytest suite instead of a one-off script.
"""

import numpy as np
import pytest

from dic_app.core.correlation import MeshParams, ROI, SolverParams, run_correlation_from_images
from dic_app.core.results import fields_to_grids, result_meta

STEP_REL_TOL = 0.10
EYY_ABS_MAX = 0.005


@pytest.fixture
def correlated(synthetic_frames):
    frames, exx_step = synthetic_frames
    roi = ROI(xc1=50, xc2=300, yc1=50, yc2=300)
    mesh = MeshParams(n_elx=3, n_ely=3, deg_e=3, deg_n=3)
    solver = SolverParams(maxit=30)
    fields = run_correlation_from_images(frames, roi, mesh, solver, upscale=2)
    return fields, roi, exx_step, len(frames)


def test_run_correlation_from_images_requires_at_least_two_frames():
    roi = ROI(0, 10, 0, 10)
    mesh = MeshParams()
    with pytest.raises(ValueError):
        run_correlation_from_images([np.zeros((20, 20))], roi, mesh)


def test_run_correlation_from_images_rejects_non_ndarray():
    roi = ROI(0, 10, 0, 10)
    mesh = MeshParams()
    with pytest.raises(TypeError):
        run_correlation_from_images(["not an array", "also not"], roi, mesh)


def test_recovered_strain_matches_imposed_deformation(correlated):
    fields, _roi, exx_step, n_frames = correlated

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


def test_displacement_grid_shapes_match_strain_grid_shapes(correlated):
    fields, _roi, _exx_step, n_frames = correlated
    disp_x = fields_to_grids(fields, "x", "displacement")
    exx = fields_to_grids(fields, "xx", "true_strain")
    assert len(disp_x) == n_frames
    assert disp_x[0].shape == exx[0].shape
    # Reference frame (frame 0) is the undeformed baseline: zero displacement/strain.
    assert np.allclose(disp_x[0], 0.0)
    assert np.allclose(exx[0], 0.0)


def test_fields_to_grids_rejects_unknown_kind(correlated):
    fields, *_ = correlated
    with pytest.raises(ValueError):
        fields_to_grids(fields, "xx", kind="not_a_real_kind")


def test_fields_to_grids_rejects_unknown_component(correlated):
    fields, *_ = correlated
    with pytest.raises(ValueError):
        fields_to_grids(fields, "zz", kind="true_strain")
    with pytest.raises(ValueError):
        fields_to_grids(fields, "not_x_or_y", kind="displacement")


def test_result_meta(correlated):
    fields, roi, _exx_step, n_frames = correlated
    meta = result_meta(fields, roi)
    assert meta.n_frames == n_frames
    assert meta.roi == (roi.xc1, roi.xc2, roi.yc1, roi.yc2)
    assert meta.scale_mm_per_px is None

"""Shared pytest fixtures: synthetic speckle frames with a known imposed strain.

Mirrors scripts/validate_mudic.py's approach so the Phase 0 gate and the
Phase 1 unit tests exercise the same trusted ground truth.
"""

import numpy as np
import pytest


@pytest.fixture
def synthetic_frames():
    """3 frames (reference + 2 deformed) with a known per-frame x-stretch.

    Returns (frames, exx_per_frame_step) where exx_per_frame_step is the
    expected true (logarithmic) strain added each frame.
    """
    from muDIC import vlab

    image_shape = (400, 400)
    exx_step = 0.05
    speckle = vlab.rosta_speckle(image_shape, dot_size=4, density=0.32, smoothness=2.0)
    F = np.array([[1.0 + exx_step, 0.0], [0.0, 1.0]])
    deformer = vlab.imageDeformer_from_defGrad(F)
    downsampler = vlab.Downsampler(image_shape=image_shape, factor=1, fill=0.8,
                                   pixel_offset_stddev=0.1)
    noise = vlab.noise_injector("gaussian", sigma=0.01)
    generator = vlab.SyntheticImageGenerator(
        speckle_image=speckle, image_deformer=deformer,
        downsampler=downsampler, noise_injector=noise, n=3,
    )
    frames = [np.asarray(generator(i)) for i in range(3)]
    return frames, float(np.log(1.0 + exx_step))

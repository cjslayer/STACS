"""µDIC correlation wrapper.

Thin, GUI-free wrapper around the canonical muDIC pipeline:

    images   = dic.image_stack_from_folder(path, file_type=...)   # or image_stack_from_list(...)
    mesher   = dic.Mesher(deg_e=3, deg_n=3, type="q4")
    mesh     = mesher.mesh(images, Xc1, Xc2, Yc1, Yc2, n_elx, n_ely, GUI=False)
    settings = dic.DICInput(mesh, images)   # ref_update, tol, maxit, ...
    job      = dic.DICAnalysis(settings)
    results  = job.run()
    fields   = dic.Fields(results, upscale=...)

The wrapper exposes ROI / mesh / solver parameters as plain values so the core
can be driven from a script, the test suite, or the GUI identically.

Verified against muDIC 0.2.1 (see scripts/validate_mudic.py and the Phase 1
smoke test run during development): fields returned by ``dic.Fields`` always
carry ``nEl == 1`` — muDIC stitches every element into one field on a
(n_elx*upscale, n_ely*upscale) grid, so callers never need to loop over
elements. See ``core/results.py`` for turning a ``Fields`` object into plain
2D numpy grids.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

import imageio.v2 as imageio
import numpy as np

import muDIC as dic


@dataclass
class ROI:
    """Rectangular region of interest, in pixels (muDIC supports rectangles only)."""
    xc1: int
    xc2: int
    yc1: int
    yc2: int


@dataclass
class MeshParams:
    n_elx: int = 9
    n_ely: int = 9
    deg_e: int = 3
    deg_n: int = 3
    elem_type: str = "q4"


@dataclass
class SolverParams:
    ref_update: List[int] = field(default_factory=list)
    maxit: int = 20
    tol: float = 1.0e-6
    interpolation_order: int = 4
    max_nr_im: Optional[int] = None
    noconvergence: str = "ignore"
    store_internals: bool = True


#: Default margin (px) default_roi() shrinks inward from the true image edge.
#: A tiny margin (a few pixels) is enough to avoid an outright IndexError --
#: muDIC's B-spline interpolation samples a few pixels beyond the mesh
#: boundary, e.g. "index 500 is out of bounds for axis 1 with size 500" --
#: but is NOT enough for the solver to actually *converge* correctly near
#: the image edge: measured empirically on synthetic 500x500 test data, an
#: 8px margin silently recovered strain ~60% low (bad convergence, not an
#: error), while 16-20px matched the imposed ground truth almost exactly.
DEFAULT_ROI_MARGIN = 20


def image_bounds(image_folder: str, file_type: str = ".png") -> ROI:
    """The TRUE full-frame ROI (pixel bounds) of the first (natsort) image found.

    Distinct from ``default_roi()``: this is what "inside the image" means
    for validation purposes, independent of any recommended-starting-ROI
    margin. Raises ``FileNotFoundError`` if no matching images are found.
    """
    matches = sorted(Path(image_folder).glob(f"*{file_type}"))
    if not matches:
        raise FileNotFoundError(f"No {file_type} images found in {image_folder}")
    image = imageio.imread(str(matches[0]))
    h, w = image.shape[:2]
    return ROI(xc1=0, xc2=w - 1, yc1=0, yc2=h - 1)


def default_roi(image_folder: str, file_type: str = ".png", margin: int = DEFAULT_ROI_MARGIN) -> ROI:
    """Recommended starting ROI: ``image_bounds()`` shrunk inward by ``margin`` px/edge.

    Shared by ``scripts/run_folder.py`` and the GUI so a picked folder gets a
    sane starting ROI without either caller re-reading image files itself.
    The margin is clamped down for very small images so xc1 < xc2 always holds.
    """
    bounds = image_bounds(image_folder, file_type)
    margin = max(0, min(margin, (bounds.xc2 - bounds.xc1 - 1) // 2, (bounds.yc2 - bounds.yc1 - 1) // 2))
    return ROI(xc1=bounds.xc1 + margin, xc2=bounds.xc2 - margin,
               yc1=bounds.yc1 + margin, yc2=bounds.yc2 - margin)


def _build_mesh(images, roi: ROI, mesh: MeshParams):
    mesher = dic.Mesher(deg_e=mesh.deg_e, deg_n=mesh.deg_n, type=mesh.elem_type)
    return mesher.mesh(
        images,
        Xc1=roi.xc1, Xc2=roi.xc2, Yc1=roi.yc1, Yc2=roi.yc2,
        n_elx=mesh.n_elx, n_ely=mesh.n_ely, GUI=False,
    )


def _run(image_stack, roi: ROI, mesh: MeshParams, solver: Optional[SolverParams], upscale: int):
    solver = solver or SolverParams()
    dic_mesh = _build_mesh(image_stack, roi, mesh)

    settings = dic.DICInput(dic_mesh, image_stack)
    settings.maxit = solver.maxit
    settings.tol = solver.tol
    settings.noconvergence = solver.noconvergence
    settings.store_internals = solver.store_internals
    settings.interpolation_order = solver.interpolation_order
    if solver.ref_update:
        settings.ref_update = solver.ref_update
    if solver.max_nr_im is not None:
        settings.max_nr_im = solver.max_nr_im

    job = dic.DICAnalysis(settings)
    results = job.run()
    return dic.Fields(results, upscale=upscale)


def run_correlation(image_folder: str,
                    roi: ROI,
                    mesh: MeshParams,
                    solver: Optional[SolverParams] = None,
                    file_type: str = ".png",
                    upscale: int = 1):
    """Run a full µDIC correlation on a folder of images. Returns a Fields object.

    Frames are read from ``image_folder`` in sorted (natsort) order; frame 0 is
    the reference (undeformed) state.
    """
    image_stack = dic.image_stack_from_folder(image_folder, file_type=file_type)
    return _run(image_stack, roi, mesh, solver, upscale)


def run_correlation_from_images(images: Sequence[np.ndarray],
                                roi: ROI,
                                mesh: MeshParams,
                                solver: Optional[SolverParams] = None,
                                upscale: int = 1):
    """Run µDIC correlation on an in-memory list of frames (e.g. a live camera feed).

    ``images[0]`` is treated as the reference (undeformed) state; every later
    frame is correlated against the growing stack, so pass frames from a live
    test in acquisition order: ``[reference, frame_1, frame_2, ...]``.

    This is the hook the live capture path (``capture/``) and the live demo
    script (``scripts/live_capture_demo.py``) call into: grab a new frame,
    append it to the list, and re-run this function to get updated full-field
    displacement/strain for the whole test so far.
    """
    if len(images) < 2:
        raise ValueError("Need at least a reference frame and one deformed frame")
    if not all(isinstance(im, np.ndarray) for im in images):
        raise TypeError("images must be a list/tuple of numpy.ndarray frames")
    image_stack = dic.image_stack_from_list(list(images))
    return _run(image_stack, roi, mesh, solver, upscale)

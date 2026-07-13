"""Normalize µDIC Fields into tidy per-frame 2D grids.

muDIC's ``Fields`` object returns arrays shaped like ``[nEl, i, j, x, y, frame]``
(tensors: strains) or ``[nEl, i, x, y, frame]`` (vectors: displacement, coords).
Verified empirically against muDIC 0.2.1: ``nEl`` is always 1 — muDIC stitches
every mesh element into one continuous field on an
``(n_elx * upscale, n_ely * upscale)`` grid before returning it, so this module
never needs to loop over elements or re-stitch anything itself. It just drops
the leading size-1 axis and hands back plain 2D numpy grids per frame/component
for the matplotlib heatmaps and CSV/npz export.

The data model carries an optional ``scale_mm_per_px`` (None for v1, strain-only)
so a pixel->mm calibration step can be added later without reworking callers.
"""

from dataclasses import dataclass
from typing import List, Optional

import numpy as np

# Vector fields: component name -> index into the "i" axis.
_VECTOR_COMPONENTS = {"x": 0, "y": 1}

# Tensor fields: component name -> (i, j) index pair.
_TENSOR_COMPONENTS = {"xx": (0, 0), "yy": (1, 1), "xy": (0, 1), "yx": (1, 0)}

_TENSOR_KINDS = {"true_strain", "eng_strain", "green_strain"}
_VECTOR_KINDS = {"displacement", "coords"}

# The standard set of fields shown/exported across the app: (label, kind,
# component, export_key). Shared so scripts/run_folder.py and dic_app/gui
# don't each define their own copy of "which fields does this app show".
STANDARD_FIELDS = [
    ("Displacement X (px)", "displacement", "x", "disp_x"),
    ("Displacement Y (px)", "displacement", "y", "disp_y"),
    ("Strain exx", "true_strain", "xx", "true_strain_xx"),
    ("Strain eyy", "true_strain", "yy", "true_strain_yy"),
    ("Strain exy", "true_strain", "xy", "true_strain_xy"),
]

# kind -> the muDIC Fields method that produces the raw array.
_FIELD_METHOD = {
    "displacement": "disp",
    "coords": "coords",
    "true_strain": "true_strain",
    "eng_strain": "eng_strain",
    "green_strain": "green_strain",
}


@dataclass
class ResultMeta:
    n_frames: int
    roi: tuple
    scale_mm_per_px: Optional[float] = None  # None => displacement reported in pixels


def fields_to_grids(fields, component: str, kind: str = "displacement") -> List[np.ndarray]:
    """Convert a muDIC ``Fields`` object + component into a list of 2D numpy grids.

    Parameters
    ----------
    fields : muDIC.Fields
        The result of ``dic.Fields(results, upscale=...)`` (see
        ``core/correlation.py``).
    component : str
        For ``kind="displacement"`` or ``"coords"``: ``"x"`` or ``"y"``.
        For a strain ``kind``: ``"xx"``, ``"yy"``, or ``"xy"``.
    kind : str
        One of ``"displacement"``, ``"coords"``, ``"true_strain"``,
        ``"eng_strain"``, ``"green_strain"``.

    Returns
    -------
    list[numpy.ndarray]
        One 2D grid (shape ``(X, Y)``) per frame, in acquisition order.
        Frame 0 (the reference frame) is included -- for displacement/strain
        it is the all-zero baseline, which is useful for sanity-checking.
    """
    if kind not in _FIELD_METHOD:
        raise ValueError(f"Unknown kind {kind!r}; expected one of {sorted(_FIELD_METHOD)}")

    raw = getattr(fields, _FIELD_METHOD[kind])()  # call the bound method, e.g. fields.disp()

    if kind in _VECTOR_KINDS:
        if component not in _VECTOR_COMPONENTS:
            raise ValueError(f"component must be one of {sorted(_VECTOR_COMPONENTS)} for kind={kind!r}")
        idx = _VECTOR_COMPONENTS[component]
        # raw: [nEl, i, x, y, frame] -> [x, y, frame]
        grid = raw[0, idx, :, :, :]
    else:
        if component not in _TENSOR_COMPONENTS:
            raise ValueError(f"component must be one of {sorted(_TENSOR_COMPONENTS)} for kind={kind!r}")
        i, j = _TENSOR_COMPONENTS[component]
        # raw: [nEl, i, j, x, y, frame] -> [x, y, frame]
        grid = raw[0, i, j, :, :, :]

    n_frames = grid.shape[-1]
    return [np.array(grid[:, :, k]) for k in range(n_frames)]


def result_meta(fields, roi, scale_mm_per_px: Optional[float] = None) -> ResultMeta:
    """Build a ``ResultMeta`` describing a completed correlation run."""
    n_frames = fields.disp().shape[-1]
    roi_tuple = (roi.xc1, roi.xc2, roi.yc1, roi.yc2) if hasattr(roi, "xc1") else tuple(roi)
    return ResultMeta(n_frames=n_frames, roi=roi_tuple, scale_mm_per_px=scale_mm_per_px)

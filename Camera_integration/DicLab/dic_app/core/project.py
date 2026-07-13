"""Project save/load: paths, ROI, mesh/solver params, and results.

Settings (this dataclass) are serialized as JSON so a test setup can be
reopened without re-picking the ROI/mesh. Field arrays (displacement/strain
grids from ``core/results.py``) are saved separately as ``.npz`` via
``save_result_grids`` / ``load_result_grids`` -- keeping the two concerns
independent means a Project can be saved before a correlation has even run.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np


@dataclass
class Project:
    image_folder: str = ""
    file_type: str = ".png"
    roi: Optional[dict] = None
    mesh: Optional[dict] = None
    solver: Optional[dict] = None

    def save(self, path: str) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: str) -> "Project":
        data = json.loads(Path(path).read_text())
        return cls(**data)


def save_result_grids(path: str, grids_by_name: Dict[str, List[np.ndarray]]) -> None:
    """Save named stacks of 2D grids (e.g. {"disp_x": [...], "true_strain_xx": [...]})
    to a single .npz file. Each list of per-frame 2D grids is stacked into one
    3D array of shape (n_frames, X, Y) before saving.
    """
    arrays = {name: np.stack(grids, axis=0) for name, grids in grids_by_name.items()}
    np.savez_compressed(path, **arrays)


def load_result_grids(path: str) -> Dict[str, List[np.ndarray]]:
    """Inverse of ``save_result_grids``: returns {name: [frame0_grid, frame1_grid, ...]}."""
    with np.load(path) as data:
        return {name: [arr[k] for k in range(arr.shape[0])] for name, arr in data.items()}

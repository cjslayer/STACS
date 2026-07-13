"""Project + result-grid persistence tests (no muDIC needed -- pure I/O roundtrips)."""

import numpy as np

from dic_app.core.project import Project, load_result_grids, save_result_grids


def test_project_save_load_roundtrip(tmp_path):
    project = Project(
        image_folder="example_data",
        file_type=".png",
        roi={"xc1": 50, "xc2": 300, "yc1": 50, "yc2": 300},
        mesh={"n_elx": 6, "n_ely": 6},
        solver={"maxit": 40},
    )
    path = tmp_path / "project.json"
    project.save(str(path))
    reloaded = Project.load(str(path))
    assert reloaded == project


def test_save_load_result_grids_roundtrip(tmp_path):
    grids = {
        "true_strain_xx": [np.random.rand(5, 5) for _ in range(3)],
        "disp_x": [np.random.rand(5, 5) for _ in range(3)],
    }
    path = tmp_path / "results.npz"
    save_result_grids(str(path), grids)
    loaded = load_result_grids(str(path))

    assert set(loaded.keys()) == set(grids.keys())
    for name, frames in grids.items():
        assert len(loaded[name]) == len(frames)
        for original, restored in zip(frames, loaded[name]):
            assert np.allclose(original, restored)

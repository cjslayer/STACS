"""GUI entry point.

Run with:  python -m dic_app.app

A single-window tkinter app over the Phase 1 headless core: pick a folder of
images OR a live camera, set ROI/mesh, run correlation, see a live strain
heatmap, export results. This is the "click and go" tool for the lab bench --
scripts/live_capture_demo.py remains available as a scriptable CLI alternative.
"""

import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np

from dic_app.capture import BaslerCameraBackend, OpenCVCameraBackend
from dic_app.core.correlation import MeshParams, ROI, SolverParams, run_correlation, run_correlation_from_images
from dic_app.core.project import save_result_grids
from dic_app.core.results import fields_to_grids
from dic_app.viz.heatmap import HeatmapCanvas

# (kind, component) options shown in the "Field" dropdown, with a friendly label.
FIELD_OPTIONS = [
    ("Strain exx", "true_strain", "xx"),
    ("Strain eyy", "true_strain", "yy"),
    ("Strain exy", "true_strain", "xy"),
    ("Displacement x (px)", "displacement", "x"),
    ("Displacement y (px)", "displacement", "y"),
]


class DicApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DIC Lab -- ISU Civil Engineering")
        self.geometry("1050x650")

        self.fields = None          # last muDIC Fields result
        self.roi = None             # ROI used for the last result
        self.n_frames = 0
        self._camera = None
        self._live_frames = []
        self._live_running = False

        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI

    def _build_layout(self):
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)

        controls = ttk.Frame(root, width=320)
        controls.pack(side="left", fill="y", padx=(0, 8))
        controls.pack_propagate(False)

        display = ttk.Frame(root)
        display.pack(side="left", fill="both", expand=True)

        self._build_controls(controls)
        self._build_display(display)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w").pack(
            side="bottom", fill="x")

    def _build_controls(self, parent):
        notebook = ttk.Notebook(parent)
        notebook.pack(fill="x")

        # --- Static (image folder) tab ----------------------------------
        static_tab = ttk.Frame(notebook, padding=8)
        notebook.add(static_tab, text="From folder")

        self.folder_var = tk.StringVar()
        ttk.Label(static_tab, text="Image folder:").grid(row=0, column=0, sticky="w")
        folder_row = ttk.Frame(static_tab)
        folder_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Entry(folder_row, textvariable=self.folder_var).pack(side="left", fill="x", expand=True)
        ttk.Button(folder_row, text="Browse...", command=self._browse_folder).pack(side="left", padx=(4, 0))

        self.file_type_var = tk.StringVar(value=".png")
        ttk.Label(static_tab, text="File type:").grid(row=2, column=0, sticky="w")
        ttk.Entry(static_tab, textvariable=self.file_type_var, width=8).grid(row=2, column=1, sticky="w")

        ttk.Button(static_tab, text="Run correlation", command=self._run_static).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        # --- Live camera tab ----------------------------------------------
        live_tab = ttk.Frame(notebook, padding=8)
        notebook.add(live_tab, text="Live camera")

        self.camera_var = tk.StringVar(value="opencv")
        ttk.Label(live_tab, text="Camera:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(live_tab, textvariable=self.camera_var, values=["opencv", "basler"],
                    state="readonly", width=10).grid(row=0, column=1, sticky="w")

        self.exposure_var = tk.StringVar(value="")
        ttk.Label(live_tab, text="Basler exposure (us):").grid(row=1, column=0, sticky="w")
        ttk.Entry(live_tab, textvariable=self.exposure_var, width=10).grid(row=1, column=1, sticky="w")

        self.interval_var = tk.DoubleVar(value=1.0)
        ttk.Label(live_tab, text="Interval (s):").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(live_tab, from_=0.1, to=60.0, increment=0.1, textvariable=self.interval_var,
                    width=10).grid(row=2, column=1, sticky="w")

        self.live_status_var = tk.StringVar(value="Not started.")
        ttk.Label(live_tab, textvariable=self.live_status_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        btn_row = ttk.Frame(live_tab)
        btn_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(btn_row, text="Start (set reference)", command=self._start_live).pack(side="left")
        ttk.Button(btn_row, text="Capture + correlate", command=self._live_tick).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Stop", command=self._stop_live).pack(side="left")

        # --- ROI / mesh (shared by both tabs) ------------------------------
        params = ttk.LabelFrame(parent, text="ROI / mesh", padding=8)
        params.pack(fill="x", pady=(8, 0))

        self.roi_vars = {k: tk.IntVar(value=v) for k, v in
                         [("xc1", 0), ("xc2", 500), ("yc1", 0), ("yc2", 500)]}
        for i, (label, key) in enumerate([("X1", "xc1"), ("X2", "xc2"), ("Y1", "yc1"), ("Y2", "yc2")]):
            ttk.Label(params, text=label).grid(row=0, column=2 * i, sticky="w")
            ttk.Entry(params, textvariable=self.roi_vars[key], width=6).grid(row=0, column=2 * i + 1)

        self.mesh_vars = {"n_elx": tk.IntVar(value=6), "n_ely": tk.IntVar(value=6)}
        ttk.Label(params, text="n_elx").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(params, textvariable=self.mesh_vars["n_elx"], width=6).grid(row=1, column=1, pady=(4, 0))
        ttk.Label(params, text="n_ely").grid(row=1, column=2, sticky="w", pady=(4, 0))
        ttk.Entry(params, textvariable=self.mesh_vars["n_ely"], width=6).grid(row=1, column=3, pady=(4, 0))

        # --- Field / frame selection + export -------------------------------
        results_box = ttk.LabelFrame(parent, text="Results", padding=8)
        results_box.pack(fill="x", pady=(8, 0))

        self.field_var = tk.StringVar(value=FIELD_OPTIONS[0][0])
        ttk.Label(results_box, text="Field:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(results_box, textvariable=self.field_var,
                    values=[o[0] for o in FIELD_OPTIONS], state="readonly").grid(
            row=0, column=1, sticky="ew", pady=(0, 4))
        self.field_var.trace_add("write", lambda *_: self._refresh_heatmap())

        self.frame_var = tk.IntVar(value=0)
        ttk.Label(results_box, text="Frame:").grid(row=1, column=0, sticky="w")
        self.frame_spin = ttk.Spinbox(results_box, from_=0, to=0, textvariable=self.frame_var,
                                      command=self._refresh_heatmap, width=6)
        self.frame_spin.grid(row=1, column=1, sticky="w")

        ttk.Button(results_box, text="Export results (.npz)", command=self._export).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def _build_display(self, parent):
        placeholder = np.zeros((2, 2))
        self.heatmap = HeatmapCanvas(parent, placeholder, title="No results yet")
        self.heatmap.pack(fill="both", expand=True)

    # ------------------------------------------------------------- actions

    def _browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.folder_var.set(folder)

    def _current_roi(self) -> ROI:
        v = self.roi_vars
        return ROI(xc1=v["xc1"].get(), xc2=v["xc2"].get(), yc1=v["yc1"].get(), yc2=v["yc2"].get())

    def _current_mesh(self) -> MeshParams:
        v = self.mesh_vars
        return MeshParams(n_elx=v["n_elx"].get(), n_ely=v["n_ely"].get())

    def _set_status(self, text: str):
        self.status_var.set(text)
        self.update_idletasks()

    def _run_static(self):
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showerror("DIC Lab", "Pick a valid image folder first.")
            return

        self._set_status("Running correlation... (this can take a while for large meshes)")

        def work():
            try:
                fields = run_correlation(
                    folder, self._current_roi(), self._current_mesh(),
                    SolverParams(maxit=40), file_type=self.file_type_var.get() or ".png",
                )
                self.after(0, lambda: self._on_correlation_done(fields))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._on_error(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_correlation_done(self, fields):
        self.fields = fields
        self.n_frames = fields.disp().shape[-1]
        self.frame_spin.configure(to=max(self.n_frames - 1, 0))
        self.frame_var.set(self.n_frames - 1)
        self._set_status(f"Done: {self.n_frames} frames.")
        self._refresh_heatmap()

    def _on_error(self, exc: Exception):
        self._set_status(f"Error: {exc}")
        messagebox.showerror("DIC Lab", str(exc))

    def _refresh_heatmap(self):
        if self.fields is None:
            return
        label = self.field_var.get()
        match = next((o for o in FIELD_OPTIONS if o[0] == label), FIELD_OPTIONS[0])
        _, kind, component = match
        frame_idx = min(self.frame_var.get(), self.n_frames - 1)
        try:
            grids = fields_to_grids(self.fields, component, kind)
        except Exception as exc:  # noqa: BLE001
            self._on_error(exc)
            return
        self.heatmap.update(grids[frame_idx], title=f"{label} -- frame {frame_idx}")

    def _export(self):
        if self.fields is None:
            messagebox.showinfo("DIC Lab", "Run a correlation first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".npz", filetypes=[("NumPy archive", "*.npz")])
        if not path:
            return
        grids = {}
        for label, kind, component in FIELD_OPTIONS:
            key = f"{kind}_{component}"
            grids[key] = fields_to_grids(self.fields, component, kind)
        save_result_grids(path, grids)
        self._set_status(f"Exported to {path}")

    # --------------------------------------------------------- live camera

    def _start_live(self):
        if self._live_running:
            self._stop_live()

        backend_name = self.camera_var.get()
        try:
            if backend_name == "basler":
                exposure = float(self.exposure_var.get()) if self.exposure_var.get().strip() else None
                self._camera = BaslerCameraBackend(exposure_time_us=exposure)
            else:
                self._camera = OpenCVCameraBackend()
            self._camera.open()
        except Exception as exc:  # noqa: BLE001
            self._on_error(exc)
            self._camera = None
            return

        try:
            reference = self._camera.grab()
        except Exception as exc:  # noqa: BLE001
            self._on_error(exc)
            self._camera.close()
            self._camera = None
            return

        self._live_frames = [reference]
        self._live_running = True
        h, w = reference.shape[:2]
        # Reset ROI to the full reference frame every time a new live session
        # starts (the previous ROI may not even fit this camera's resolution).
        # Edit the X1/X2/Y1/Y2 fields afterward to narrow to a sub-region.
        self.roi_vars["xc1"].set(0)
        self.roi_vars["xc2"].set(w - 1)
        self.roi_vars["yc1"].set(0)
        self.roi_vars["yc2"].set(h - 1)
        self.live_status_var.set(f"Reference frame captured ({w}x{h}). Ready to capture test frames.")
        self._set_status("Live session started.")

    def _live_tick(self):
        """Grab one more frame and re-run correlation over the whole history."""
        if not self._live_running or self._camera is None:
            messagebox.showinfo("DIC Lab", "Click 'Start (set reference)' first.")
            return
        try:
            frame = self._camera.grab()
        except Exception as exc:  # noqa: BLE001
            self._on_error(exc)
            return
        self._live_frames.append(frame)
        self.live_status_var.set(f"{len(self._live_frames)} frames captured. Correlating...")
        self._set_status("Correlating live frame history...")

        frames = list(self._live_frames)
        roi = self._current_roi()
        mesh = self._current_mesh()

        def work():
            try:
                fields = run_correlation_from_images(frames, roi, mesh, SolverParams(maxit=40))
                self.after(0, lambda: self._on_live_correlation_done(fields))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._on_error(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_live_correlation_done(self, fields):
        self._on_correlation_done(fields)
        self.live_status_var.set(f"{len(self._live_frames)} frames captured.")

    def _stop_live(self):
        self._live_running = False
        if self._camera is not None:
            try:
                self._camera.close()
            except Exception:  # noqa: BLE001
                pass
            self._camera = None
        self.live_status_var.set(f"Stopped ({len(self._live_frames)} frames captured).")

    def _on_close(self):
        self._stop_live()
        self.destroy()


def main() -> None:
    app = DicApp()
    app.mainloop()


if __name__ == "__main__":
    sys.exit(main())

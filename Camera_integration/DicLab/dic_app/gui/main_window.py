"""Static image-folder DIC GUI: import -> ROI/mesh -> run -> results.

Import a folder of speckle images, run muDIC correlation, and view/export
displacement + strain heatmaps. No live camera here (see ``dic_app.app`` for
the camera-capable GUI) -- this window is the core-pipeline-only tool.

Every action in this window calls straight into the same headless core
(``dic_app.core.correlation``, ``dic_app.core.project``,
``dic_app.core.results``) that ``scripts/run_folder.py`` uses, so results are
identical whether you use the GUI or the CLI -- nothing is reimplemented here.

Run with:  python -m dic_app.gui
"""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import numpy as np

from dic_app.core.correlation import MeshParams, ROI, SolverParams, default_roi, image_bounds, run_correlation
from dic_app.core.project import save_result_grids
from dic_app.core.results import STANDARD_FIELDS, fields_to_grids
from dic_app.gui.live_capture_panel import LiveCapturePanel
from dic_app.viz.heatmap import HeatmapCanvas, save_heatmap

# --- style reference: ISU lab data analyzer look -----------------------
BG = "#F4F5F7"
CARD_BG = "#FFFFFF"
ACCENT = "#1E3A5F"
ACCENT_ACTIVE = "#16293F"
TEXT = "#1B1F23"
MUTED = "#5B6572"
FONT = "Segoe UI"


def _apply_theme(root: tk.Tk) -> None:
    root.configure(background=BG)
    style = ttk.Style(root)
    # "clam" actually honors custom colors on Windows; the native themes
    # (vista/xpnative) silently ignore most background/foreground overrides.
    style.theme_use("clam")

    style.configure("TFrame", background=BG)
    style.configure("CardInner.TFrame", background=CARD_BG)
    style.configure("Card.TFrame", background=CARD_BG, relief="solid", borderwidth=1)
    style.configure("HeaderBar.TFrame", background=ACCENT)

    style.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 10))
    style.configure("Card.TLabel", background=CARD_BG, foreground=TEXT, font=(FONT, 10))
    style.configure("CardHeader.TLabel", background=CARD_BG, foreground=ACCENT, font=(FONT, 11, "bold"))
    style.configure("Muted.TLabel", background=CARD_BG, foreground=MUTED, font=(FONT, 9))
    style.configure("Status.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))
    style.configure("Header.TLabel", background=ACCENT, foreground="white", font=(FONT, 15, "bold"))
    style.configure("HeaderSub.TLabel", background=ACCENT, foreground="#C7D2DE", font=(FONT, 10))

    style.configure("TButton", font=(FONT, 10), padding=6)
    style.configure("Accent.TButton", background=ACCENT, foreground="white", font=(FONT, 10, "bold"), padding=6)
    style.map(
        "Accent.TButton",
        background=[("disabled", "#A9B4C0"), ("active", ACCENT_ACTIVE)],
        foreground=[("disabled", "#EEF1F4")],
    )
    style.map("TButton", background=[("active", "#E4E7EB")])

    style.configure("TEntry", fieldbackground="white")
    style.configure("TCombobox", fieldbackground="white")
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#E4E7EB")

    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=BG, foreground=TEXT, font=(FONT, 10), padding=(14, 8))
    style.map("TNotebook.Tab",
              background=[("selected", CARD_BG)],
              foreground=[("selected", ACCENT)])


class DicGuiApp(tk.Tk):
    """Main window: load a folder, run correlation, inspect/export results."""

    def __init__(self):
        super().__init__()
        self.title("DIC Lab -- ISU Civil Engineering")
        self.geometry("1150x680")
        self.minsize(940, 600)
        _apply_theme(self)

        self.folder = None
        self.fields = None          # last muDIC Fields result
        self.n_frames = 0
        self._running = False
        self._image_bounds = None   # full-frame ROI of the loaded folder, for ROI validation

        self.file_type_var = tk.StringVar(value=".png")
        self.roi_vars = {k: tk.IntVar(value=v) for k, v in
                          [("xc1", 0), ("xc2", 500), ("yc1", 0), ("yc2", 500)]}
        self.mesh_vars = {"n_elx": tk.IntVar(value=8), "n_ely": tk.IntVar(value=8)}
        self.folder_var = tk.StringVar(value="No folder selected.")
        self.status_var = tk.StringVar(value="Ready. Load an image folder to begin.")
        self.running_var = tk.StringVar(value="")
        self.field_var = tk.StringVar(value=STANDARD_FIELDS[2][0])  # default: Strain exx
        self.frame_var = tk.IntVar(value=0)

        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI

    def _build_layout(self):
        header = ttk.Frame(self, style="HeaderBar.TFrame", padding=(16, 12))
        header.pack(fill="x")
        ttk.Label(header, text="DIC Lab", style="Header.TLabel").pack(side="left")
        ttk.Label(header, text="Digital Image Correlation -- ISU Civil Engineering",
                  style="HeaderSub.TLabel").pack(side="left", padx=(12, 0))

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(12, 0))

        import_tab = ttk.Frame(self.notebook, padding=(0, 12, 0, 0))
        self.notebook.add(import_tab, text="Import Folder")
        self._build_setup_card(import_tab)
        self._build_results_card(import_tab)

        live_tab = ttk.Frame(self.notebook, padding=(0, 12, 0, 0))
        self.notebook.add(live_tab, text="Live Capture")
        self.live_panel = LiveCapturePanel(live_tab, on_capture_saved=self._on_live_capture_saved)
        self.live_panel.pack(fill="both", expand=True)

        status_bar = ttk.Frame(self, padding=(12, 6))
        status_bar.pack(fill="x", side="bottom")
        ttk.Label(status_bar, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(status_bar, mode="indeterminate", length=160)

    def _build_setup_card(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16, width=320)
        card.pack(side="left", fill="y", padx=(0, 12))
        card.pack_propagate(False)

        ttk.Label(card, text="1. Import", style="CardHeader.TLabel").pack(anchor="w")
        ttk.Separator(card).pack(fill="x", pady=(4, 10))

        ttk.Label(card, textvariable=self.folder_var, style="Muted.TLabel",
                  wraplength=280).pack(anchor="w", fill="x")

        type_row = ttk.Frame(card, style="CardInner.TFrame")
        type_row.pack(fill="x", pady=(8, 4))
        ttk.Label(type_row, text="File type:", style="Card.TLabel").pack(side="left")
        ttk.Combobox(type_row, textvariable=self.file_type_var,
                     values=[".png", ".bmp", ".tif", ".tiff"], state="readonly", width=8
                     ).pack(side="left", padx=(6, 0))

        ttk.Button(card, text="Load Image Folder", style="Accent.TButton",
                   command=self._on_load_folder_clicked).pack(fill="x", pady=(10, 18))

        ttk.Label(card, text="2. Region + mesh", style="CardHeader.TLabel").pack(anchor="w")
        ttk.Separator(card).pack(fill="x", pady=(4, 10))

        roi_grid = ttk.Frame(card, style="CardInner.TFrame")
        roi_grid.pack(fill="x")
        for i, (label, key) in enumerate([("X1", "xc1"), ("X2", "xc2"), ("Y1", "yc1"), ("Y2", "yc2")]):
            ttk.Label(roi_grid, text=label, style="Card.TLabel").grid(
                row=i // 2, column=(i % 2) * 2, sticky="w", pady=2)
            ttk.Entry(roi_grid, textvariable=self.roi_vars[key], width=8).grid(
                row=i // 2, column=(i % 2) * 2 + 1, padx=(4, 14))

        mesh_row = ttk.Frame(card, style="CardInner.TFrame")
        mesh_row.pack(fill="x", pady=(10, 0))
        ttk.Label(mesh_row, text="Mesh nx", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(mesh_row, textvariable=self.mesh_vars["n_elx"], width=6).grid(row=0, column=1, padx=(4, 14))
        ttk.Label(mesh_row, text="Mesh ny", style="Card.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Entry(mesh_row, textvariable=self.mesh_vars["n_ely"], width=6).grid(row=0, column=3, padx=(4, 0))

        ttk.Label(card, text="3. Correlate", style="CardHeader.TLabel").pack(anchor="w", pady=(18, 0))
        ttk.Separator(card).pack(fill="x", pady=(4, 10))

        self.run_button = ttk.Button(card, text="Run Correlation", style="Accent.TButton",
                                      command=self._on_run_clicked)
        self.run_button.pack(fill="x")
        ttk.Label(card, textvariable=self.running_var, style="Muted.TLabel",
                  wraplength=280).pack(anchor="w", pady=(6, 0))

        ttk.Label(card, text="4. Export", style="CardHeader.TLabel").pack(anchor="w", pady=(18, 0))
        ttk.Separator(card).pack(fill="x", pady=(4, 10))
        self.export_button = ttk.Button(card, text="Export Results...", state="disabled",
                                         command=self._on_export_clicked)
        self.export_button.pack(fill="x")

    def _build_results_card(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.pack(side="left", fill="both", expand=True)

        ttk.Label(card, text="Results", style="CardHeader.TLabel").pack(anchor="w")
        ttk.Separator(card).pack(fill="x", pady=(4, 10))

        controls = ttk.Frame(card, style="CardInner.TFrame")
        controls.pack(fill="x", pady=(0, 8))
        ttk.Label(controls, text="Field:", style="Card.TLabel").pack(side="left")
        field_box = ttk.Combobox(controls, textvariable=self.field_var,
                                  values=[f[0] for f in STANDARD_FIELDS], state="readonly", width=20)
        field_box.pack(side="left", padx=(6, 16))
        self.field_var.trace_add("write", lambda *_: self._refresh_heatmap())

        ttk.Label(controls, text="Frame:", style="Card.TLabel").pack(side="left")
        self.frame_spin = ttk.Spinbox(controls, from_=0, to=0, textvariable=self.frame_var,
                                       command=self._refresh_heatmap, width=6)
        self.frame_spin.pack(side="left", padx=(6, 0))

        display = ttk.Frame(card, style="CardInner.TFrame")
        display.pack(fill="both", expand=True)
        placeholder = np.zeros((2, 2))
        self.heatmap = HeatmapCanvas(display, placeholder, title="No results yet -- load a folder and run correlation")
        self.heatmap.pack(fill="both", expand=True)

    # --------------------------------------------------------------- state

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _show_error(self, message: str) -> None:
        self._set_status(f"Error: {message.splitlines()[0]}")
        messagebox.showerror("DIC Lab", message)

    def _current_roi(self) -> ROI:
        v = self.roi_vars
        return ROI(xc1=v["xc1"].get(), xc2=v["xc2"].get(), yc1=v["yc1"].get(), yc2=v["yc2"].get())

    def _current_mesh(self) -> MeshParams:
        v = self.mesh_vars
        return MeshParams(n_elx=v["n_elx"].get(), n_ely=v["n_ely"].get())

    def _set_running(self, running: bool) -> None:
        self._running = running
        state = "disabled" if running else "normal"
        self.run_button.configure(state=state)
        self.export_button.configure(state="disabled" if running or self.fields is None else "normal")
        if running:
            self.running_var.set("Running correlation... this can take a while for large meshes. Please wait.")
            self.progress.pack(side="right")
            self.progress.start(12)
            self._set_status("Running correlation...")
        else:
            self.running_var.set("")
            self.progress.stop()
            self.progress.pack_forget()

    # ------------------------------------------------------------- actions

    def _on_load_folder_clicked(self) -> None:
        folder = filedialog.askdirectory(title="Select speckle image folder")
        if folder:
            self._apply_folder(folder)

    def _apply_folder(self, folder: str) -> bool:
        """Validate and adopt an image folder. Returns True on success.

        Split out from the click handler so it can be driven directly (by
        tests, or a future "recent folders" list) without going through the
        modal folder-picker dialog.
        """
        file_type = self.file_type_var.get()
        try:
            bounds = image_bounds(folder, file_type)
            roi = default_roi(folder, file_type)
            count = len(list(Path(folder).glob(f"*{file_type}")))
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"Could not load images from this folder:\n\n{exc}")
            return False

        self.folder = folder
        self._image_bounds = bounds  # TRUE image pixel bounds, for _validate_roi
        self.folder_var.set(f"{folder}\n({count} {file_type} images found)")
        for key, value in [("xc1", roi.xc1), ("xc2", roi.xc2), ("yc1", roi.yc1), ("yc2", roi.yc2)]:
            self.roi_vars[key].set(value)
        self._set_status(f"Loaded {count} images. Ready to run correlation.")
        return True

    def _validate_roi(self, roi: ROI) -> Optional[str]:
        """Return an error message if ``roi`` is unusable, else None.

        muDIC does not fail fast on an out-of-bounds ROI -- it can spend
        minutes building/solving a nonsensical mesh before erroring (or
        possibly never finishing), which looks like a hang to the user.
        Checking against the actual image bounds (NOT the shrunk default_roi()
        recommendation -- a user is allowed to widen the ROI back toward the
        real edge) up front avoids that.
        """
        if roi.xc1 >= roi.xc2 or roi.yc1 >= roi.yc2:
            return "ROI is invalid: X1 must be less than X2, and Y1 must be less than Y2."
        if self._image_bounds is not None:
            b = self._image_bounds
            if roi.xc1 < b.xc1 or roi.xc2 > b.xc2 or roi.yc1 < b.yc1 or roi.yc2 > b.yc2:
                return (f"ROI (X {roi.xc1}-{roi.xc2}, Y {roi.yc1}-{roi.yc2}) falls outside "
                        f"the loaded image (X {b.xc1}-{b.xc2}, Y {b.yc1}-{b.yc2}).")
        return None

    def _on_run_clicked(self) -> None:
        if self._running:
            return
        if not self.folder:
            self._show_error("Load an image folder first.")
            return
        try:
            roi = self._current_roi()
            mesh = self._current_mesh()
        except tk.TclError:
            self._show_error("ROI and mesh fields must be whole numbers.")
            return

        roi_error = self._validate_roi(roi)
        if roi_error:
            self._show_error(roi_error)
            return

        folder = self.folder
        file_type = self.file_type_var.get()
        self._set_running(True)

        def work():
            try:
                fields = run_correlation(folder, roi, mesh, SolverParams(maxit=40), file_type=file_type)
            except Exception as exc:  # noqa: BLE001
                # CPython deletes the `except ... as exc` binding when the
                # except suite exits, which happens before this lambda ever
                # runs (it's scheduled async via .after()) -- default-arg
                # bind it now so the closure doesn't see a NameError instead.
                self.after(0, lambda exc=exc: self._on_run_error(exc))
                return
            self.after(0, lambda: self._on_run_done(fields))

        threading.Thread(target=work, daemon=True).start()

    def _on_run_done(self, fields) -> None:
        self.fields = fields
        self.n_frames = fields.disp().shape[-1]
        self._set_running(False)
        self.frame_spin.configure(to=max(self.n_frames - 1, 0))
        self.frame_var.set(self.n_frames - 1)
        self._set_status(f"Done: {self.n_frames} frames correlated.")
        self._refresh_heatmap()

    def _on_run_error(self, exc: Exception) -> None:
        self._set_running(False)
        if isinstance(exc, IndexError):
            # muDIC's interpolation samples a few pixels beyond the mesh
            # edge; an ROI touching the image boundary trips this deep
            # inside the solver as a raw IndexError. default_roi() now
            # leaves a margin automatically, but a manually-widened ROI can
            # still hit it -- translate it into something actionable.
            self._show_error(
                "Correlation failed: the ROI is too close to the image edge.\n\n"
                "muDIC's interpolation needs a margin of roughly 20+ pixels between "
                "the ROI and the image boundary to converge correctly. Shrink X2/Y2 "
                "(and/or increase X1/Y1) by at least 20px on the affected edge(s) and "
                f"try again.\n\nDetails: {exc}"
            )
            return
        self._show_error(f"Correlation failed:\n\n{exc}")

    def _refresh_heatmap(self) -> None:
        if self.fields is None:
            return
        label = self.field_var.get()
        match = next((f for f in STANDARD_FIELDS if f[0] == label), STANDARD_FIELDS[0])
        _label, kind, component, _name = match
        try:
            frame_idx = min(self.frame_var.get(), self.n_frames - 1) if self.n_frames else 0
        except tk.TclError:
            self._show_error("Frame must be a whole number.")
            return
        try:
            grids = fields_to_grids(self.fields, component, kind)
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"Could not render heatmap:\n\n{exc}")
            return
        self.heatmap.update(grids[frame_idx], title=f"{label} -- frame {frame_idx}")

    def _on_export_clicked(self) -> None:
        if self.fields is None:
            messagebox.showinfo("DIC Lab", "Run a correlation first.")
            return
        out_dir = filedialog.askdirectory(title="Choose a folder to export results to")
        if out_dir:
            self._export_to(out_dir)

    def _export_to(self, out_dir: str) -> bool:
        try:
            grids_by_name = {name: fields_to_grids(self.fields, component, kind)
                              for _label, kind, component, name in STANDARD_FIELDS}
            out_path = Path(out_dir)
            out_path.mkdir(parents=True, exist_ok=True)
            save_result_grids(str(out_path / "dic_results.npz"), grids_by_name)

            heatmap_dir = out_path / "heatmaps"
            heatmap_dir.mkdir(exist_ok=True)
            for name, grids in grids_by_name.items():
                save_heatmap(grids[-1], str(heatmap_dir / f"{name}.png"), title=f"{name} -- last frame")
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"Export failed:\n\n{exc}")
            return False

        self._set_status(f"Exported results to {out_dir}")
        messagebox.showinfo("DIC Lab", f"Exported dic_results.npz and heatmaps/ to:\n{out_dir}")
        return True

    # ------------------------------------------------------------ live capture

    def _on_live_capture_saved(self, folder: str, n_frames: int) -> None:
        """Called by LiveCapturePanel when a capture session stops with >=2 frames."""
        self._set_status(f"Captured {n_frames} frames to {folder}.")
        if messagebox.askyesno(
            "DIC Lab",
            f"Captured {n_frames} frames to:\n{folder}\n\n"
            "Load them into the Import Folder tab now?",
        ):
            self.notebook.select(0)
            self.file_type_var.set(".tif")  # matches LiveCapturePanel's save format
            self._apply_folder(folder)

    def _on_close(self) -> None:
        self.live_panel.shutdown()
        self.destroy()


def main() -> None:
    app = DicGuiApp()
    app.mainloop()


if __name__ == "__main__":
    main()

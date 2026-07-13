"""Live Capture tab: camera preview + frame-to-disk capture.

Phase 2 scope only: a live PREVIEW plus capture-to-disk. Correlation is NOT
run continuously while capturing -- that's Phase 3. Captured frames are meant
to be picked up afterward by the existing "Import Folder" tab, which runs the
exact same ``core.correlation.run_correlation`` pipeline already proven
against real files on disk (see ``dic_app.gui.main_window``).

Builds on the existing ``dic_app.capture`` backend abstraction (do not
duplicate it here): any ``CameraBackend`` -- ``OpenCVCameraBackend`` today,
``BaslerCameraBackend`` later -- is a drop-in frame source. Swapping backends
is a one-line change in ``_open_camera`` below.

Threading model: a single background "producer" thread owns the camera and
calls ``backend.grab()`` in a tight loop (this can block, e.g. waiting on
hardware). It never touches Tk widgets directly. It just updates a
lock-guarded "latest frame" + capture counters, and (while capturing) writes
frames to disk. The Tk main thread polls those shared values on a recurring
``after()`` timer to refresh the preview image and status label -- the same
safe cross-thread pattern ``main_window.py`` uses for the correlation worker,
just repeating instead of one-shot.
"""

import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageTk

from dic_app.capture import BaslerCameraBackend, OpenCVCameraBackend

CAMERA_BACKENDS = ["OpenCV webcam", "Basler (pypylon)"]
PREVIEW_MAX_SIZE = (480, 480)
PREVIEW_POLL_MS = 66  # ~15 fps UI refresh; independent of the camera's own grab rate


class LiveCapturePanel(ttk.Frame):
    """Self-contained live-capture UI: open camera -> preview -> start/stop capture-to-disk.

    Reports a finished capture session via ``on_capture_saved(folder, n_frames)``
    so the host window can offer to load it straight into the Import Folder tab.
    """

    def __init__(self, parent, on_capture_saved=None, **kwargs):
        super().__init__(parent, style="TFrame", **kwargs)
        self._on_capture_saved = on_capture_saved

        self._camera = None
        self._preview_thread = None
        self._stop_preview = threading.Event()
        self._lock = threading.Lock()
        self._latest_frame = None

        self._capturing = False
        self._capture_dir = None
        self._capture_count = 0
        self._capture_start_time = None
        self._last_saved_time = None

        self._preview_photo = None  # keep a live reference; Tk drops GC'd PhotoImages

        style = ttk.Style()
        style.configure("Preview.TLabel", background="#111318")

        self._build_layout()

    # ------------------------------------------------------------------ UI

    def _build_layout(self):
        controls = ttk.Frame(self, style="Card.TFrame", padding=16, width=320)
        controls.pack(side="left", fill="y", padx=(0, 12))
        controls.pack_propagate(False)

        ttk.Label(controls, text="1. Camera", style="CardHeader.TLabel").pack(anchor="w")
        ttk.Separator(controls).pack(fill="x", pady=(4, 10))

        self.backend_var = tk.StringVar(value=CAMERA_BACKENDS[0])
        ttk.Combobox(controls, textvariable=self.backend_var, values=CAMERA_BACKENDS,
                     state="readonly", width=22).pack(fill="x", pady=(0, 6))

        device_row = ttk.Frame(controls, style="CardInner.TFrame")
        device_row.pack(fill="x", pady=(0, 4))
        ttk.Label(device_row, text="Device index:", style="Card.TLabel").pack(side="left")
        self.device_index_var = tk.IntVar(value=0)
        ttk.Entry(device_row, textvariable=self.device_index_var, width=6).pack(side="left", padx=(6, 0))

        exposure_row = ttk.Frame(controls, style="CardInner.TFrame")
        exposure_row.pack(fill="x", pady=(0, 4))
        ttk.Label(exposure_row, text="Basler exposure (us):", style="Card.TLabel").pack(side="left")
        self.exposure_var = tk.StringVar(value="")
        ttk.Entry(exposure_row, textvariable=self.exposure_var, width=8).pack(side="left", padx=(6, 0))

        self.open_button = ttk.Button(controls, text="Open Camera", style="Accent.TButton",
                                       command=self._on_open_clicked)
        self.open_button.pack(fill="x", pady=(8, 4))
        self.close_button = ttk.Button(controls, text="Close Camera", state="disabled",
                                        command=self._on_close_camera_clicked)
        self.close_button.pack(fill="x")

        ttk.Label(controls, text="2. Capture", style="CardHeader.TLabel").pack(anchor="w", pady=(18, 0))
        ttk.Separator(controls).pack(fill="x", pady=(4, 10))

        ttk.Label(controls, text="Save frames to:", style="Card.TLabel").pack(anchor="w")
        dir_row = ttk.Frame(controls, style="CardInner.TFrame")
        dir_row.pack(fill="x", pady=(2, 6))
        self.capture_dir_var = tk.StringVar(value=self._default_capture_dir())
        ttk.Entry(dir_row, textvariable=self.capture_dir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(dir_row, text="...", width=3, command=self._on_browse_capture_dir).pack(side="left", padx=(4, 0))

        interval_row = ttk.Frame(controls, style="CardInner.TFrame")
        interval_row.pack(fill="x", pady=(0, 8))
        ttk.Label(interval_row, text="Interval (s):", style="Card.TLabel").pack(side="left")
        self.interval_var = tk.DoubleVar(value=1.0)
        ttk.Spinbox(interval_row, from_=0.1, to=60.0, increment=0.1,
                    textvariable=self.interval_var, width=8).pack(side="left", padx=(6, 0))

        self.start_capture_button = ttk.Button(controls, text="Start Capture", style="Accent.TButton",
                                                state="disabled", command=self._on_start_capture_clicked)
        self.start_capture_button.pack(fill="x", pady=(0, 4))
        self.stop_capture_button = ttk.Button(controls, text="Stop Capture", state="disabled",
                                               command=self._on_stop_capture_clicked)
        self.stop_capture_button.pack(fill="x")

        self.status_var = tk.StringVar(value="Camera not open.")
        ttk.Label(controls, textvariable=self.status_var, style="Muted.TLabel",
                  wraplength=280).pack(anchor="w", pady=(10, 0))

        display = ttk.Frame(self, style="Card.TFrame", padding=16)
        display.pack(side="left", fill="both", expand=True)
        ttk.Label(display, text="Preview", style="CardHeader.TLabel").pack(anchor="w")
        ttk.Separator(display).pack(fill="x", pady=(4, 10))

        self.preview_label = ttk.Label(display, style="Preview.TLabel", anchor="center",
                                        text="Open a camera to see a live preview.", foreground="#8A93A0")
        self.preview_label.pack(fill="both", expand=True)

    @staticmethod
    def _default_capture_dir() -> str:
        return str(Path("captures") / time.strftime("capture_%Y%m%d_%H%M%S"))

    # --------------------------------------------------------------- camera

    def _build_backend(self):
        name = self.backend_var.get()
        if name == CAMERA_BACKENDS[1]:
            exposure = float(self.exposure_var.get()) if self.exposure_var.get().strip() else None
            return BaslerCameraBackend(exposure_time_us=exposure)
        return OpenCVCameraBackend(device_index=self.device_index_var.get())

    def _on_open_clicked(self):
        try:
            camera = self._build_backend()
            camera.open()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("DIC Lab", f"Could not open camera:\n\n{exc}")
            return

        self._camera = camera
        self._stop_preview.clear()
        self._preview_thread = threading.Thread(target=self._preview_loop, daemon=True)
        self._preview_thread.start()

        self.open_button.configure(state="disabled")
        self.close_button.configure(state="normal")
        self.start_capture_button.configure(state="normal")
        self.status_var.set("Preview running.")
        self.after(PREVIEW_POLL_MS, self._tick)

    def _on_close_camera_clicked(self):
        self._shutdown_camera()
        self.status_var.set("Camera not open.")

    def _shutdown_camera(self):
        """Stop the preview thread and release the camera. Safe to call repeatedly."""
        if self._capturing:
            self._finish_capture_session()
        self._stop_preview.set()
        if self._preview_thread is not None:
            self._preview_thread.join(timeout=5.0)
            self._preview_thread = None
        if self._camera is not None:
            try:
                self._camera.close()
            except Exception:  # noqa: BLE001
                pass
            self._camera = None
        self.open_button.configure(state="normal")
        self.close_button.configure(state="disabled")
        self.start_capture_button.configure(state="disabled")
        self.stop_capture_button.configure(state="disabled")

    def _preview_loop(self):
        """Runs on the background thread; never touches Tk widgets directly."""
        while not self._stop_preview.is_set():
            try:
                frame = self._camera.grab()
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda exc=exc: self._on_camera_error(exc))
                return
            now = time.time()
            with self._lock:
                self._latest_frame = frame
                if self._capturing:
                    due = self._last_saved_time is None or (now - self._last_saved_time) >= self.interval_var.get()
                    if due:
                        self._save_frame_locked(frame)
                        self._last_saved_time = now

    def _save_frame_locked(self, frame: np.ndarray) -> None:
        """Write one frame to disk. Caller must hold ``self._lock``."""
        path = self._capture_dir / f"frame_{self._capture_count:03d}.tif"
        imageio.imwrite(str(path), frame)
        self._capture_count += 1

    def _on_camera_error(self, exc: Exception) -> None:
        # The producer thread has already returned by the time this fires.
        self._preview_thread = None
        was_capturing = self._capturing
        self._shutdown_camera()
        prefix = "Capture" if was_capturing else "Preview"
        messagebox.showerror("DIC Lab", f"{prefix} stopped: camera error.\n\n{exc}")
        self.status_var.set(f"Error: {exc}")

    # -------------------------------------------------------------- capture

    def _on_browse_capture_dir(self):
        folder = filedialog.askdirectory(title="Choose a folder to save captured frames")
        if folder:
            self.capture_dir_var.set(folder)

    def _on_start_capture_clicked(self):
        if self._camera is None:
            messagebox.showerror("DIC Lab", "Open a camera first.")
            return
        try:
            capture_dir = Path(self.capture_dir_var.get())
            capture_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("DIC Lab", f"Could not create capture folder:\n\n{exc}")
            return

        with self._lock:
            self._capture_dir = capture_dir
            self._capture_count = 0
            self._capture_start_time = time.time()
            self._last_saved_time = None  # save the current frame immediately, as the reference
            self._capturing = True

        self.start_capture_button.configure(state="disabled")
        self.stop_capture_button.configure(state="normal")
        self.status_var.set(f"Capturing to {capture_dir}...")

    def _on_stop_capture_clicked(self):
        folder, count = self._finish_capture_session()
        self.start_capture_button.configure(state="normal" if self._camera is not None else "disabled")
        self.stop_capture_button.configure(state="disabled")
        if count >= 2:
            self.status_var.set(f"Captured {count} frames to {folder}.")
            if self._on_capture_saved is not None:
                self._on_capture_saved(str(folder), count)
        elif count == 1:
            self.status_var.set(f"Only 1 frame captured to {folder} -- need at least 2 to correlate.")
        else:
            self.status_var.set("Capture stopped -- no frames were saved.")

    def _finish_capture_session(self):
        with self._lock:
            self._capturing = False
            folder, count = self._capture_dir, self._capture_count
        return folder, count

    # ------------------------------------------------------------ UI ticks

    def _tick(self):
        if self._camera is None:
            return  # camera closed -- stop rescheduling
        with self._lock:
            frame = self._latest_frame
            capturing = self._capturing
            count = self._capture_count
            start = self._capture_start_time
        if frame is not None:
            self._update_preview(frame)
        if capturing and start is not None:
            elapsed = max(time.time() - start, 1e-6)
            rate = count / elapsed
            self.status_var.set(f"Capturing... {count} frames saved ({rate:.2f} fps, {elapsed:.1f}s elapsed)")
        elif count:
            self.status_var.set(f"Preview running. {count} frames captured last session.")
        self.after(PREVIEW_POLL_MS, self._tick)

    def _update_preview(self, frame: np.ndarray) -> None:
        image = Image.fromarray(frame, mode="L") if frame.ndim == 2 else Image.fromarray(frame)
        image.thumbnail(PREVIEW_MAX_SIZE)
        self._preview_photo = ImageTk.PhotoImage(image)
        self.preview_label.configure(image=self._preview_photo, text="")

    # --------------------------------------------------------------- public

    def shutdown(self) -> None:
        """Called by the host window before it destroys itself."""
        self._shutdown_camera()

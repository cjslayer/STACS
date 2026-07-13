"""Displacement/strain heatmaps via matplotlib FigureCanvasTkAgg.

Renders our own heatmaps from normalized numpy grids (results.fields_to_grids)
rather than using muDIC's debug Visualizer, to match the lab's existing
tkinter + matplotlib look and to keep the field rendering under our control.
"""

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


def save_heatmap(grid, path, title: str = "", cmap: str = "viridis") -> None:
    """Render a grid to a static heatmap PNG via the Agg backend -- no Tk/display needed.

    Used by headless scripts (e.g. ``scripts/run_folder.py``) and tests to
    produce/inspect heatmaps without going through the tkinter GUI.
    """
    grid = np.asarray(grid)
    figure = Figure(figsize=(5, 4), dpi=100)
    FigureCanvasAgg(figure)
    ax = figure.add_subplot(111)
    image = ax.imshow(grid.T, cmap=cmap, origin="lower")
    figure.colorbar(image, ax=ax)
    ax.set_title(title)
    figure.tight_layout()
    figure.savefig(path)


class HeatmapCanvas:
    """An embeddable matplotlib heatmap + colorbar that updates in place.

    ``update(grid)`` refreshes the displayed data without rebuilding the
    figure -- this is what a live capture loop calls on every new frame.
    """

    def __init__(self, parent, grid, title: str = "", cmap: str = "viridis"):
        grid = np.asarray(grid)
        self.figure = Figure(figsize=(5, 4), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self.image = self.ax.imshow(grid.T, cmap=cmap, origin="lower")
        self.colorbar = self.figure.colorbar(self.image, ax=self.ax)
        self.ax.set_title(title)
        self.figure.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.figure, master=parent)
        self.widget = self.canvas.get_tk_widget()

    def update(self, grid, title: str = None) -> None:
        grid = np.asarray(grid)
        self.image.set_data(grid.T)
        vmin = float(np.nanmin(grid)) if grid.size else 0.0
        vmax = float(np.nanmax(grid)) if grid.size else 1.0
        if vmax <= vmin:
            vmax = vmin + 1e-9
        self.image.set_clim(vmin=vmin, vmax=vmax)
        if title is not None:
            self.ax.set_title(title)
        self.canvas.draw_idle()

    def pack(self, **kwargs) -> "HeatmapCanvas":
        self.widget.pack(**kwargs)
        return self


def make_heatmap_canvas(parent, grid, title: str = "", cmap: str = "viridis") -> HeatmapCanvas:
    """Create a FigureCanvasTkAgg heatmap with a colorbar."""
    return HeatmapCanvas(parent, grid, title=title, cmap=cmap)

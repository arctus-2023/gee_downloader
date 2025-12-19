"""QA quicklooks for geospatial outputs.

Goal
----
Create an easy-to-inspect quicklook image that overlays a raster product on top
of a basemap and draws the AOI / raster footprint.

Why this exists
---------------
When debugging georegistration issues (rotations, offsets, wrong transforms),
opening a GeoTIFF in QGIS is great but slow for iterative runs. This module
creates a compact PNG (and optional HTML) artifact so you can quickly confirm:

- raster is in the expected place
- raster is rotated correctly
- AOI clipping occurred
- CRS/extent/transform seem sensible

Design constraints
------------------
- Do not require remote map tile downloads (often blocked in secure envs).
- Prefer a local background if possible. If optional deps exist, we can add a
  basemap from Cartopy (Natural Earth) which is packaged with the library.

If Cartopy or Matplotlib are unavailable, callers can still use the JSON
metadata helper.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuicklookRequest:
    raster_path: str
    output_png: str
    title: str = ""
    # AOI bbox in EPSG:4326
    aoi_bbox: Optional[list[float]] = None
    # Expand the view by this many percent of raster span in each direction.
    # Example: 0.25 gives a 25% margin.
    margin_fraction: float = 0.25
    # Sample size for display (max dimension in pixels). Kept small to avoid
    # memory spikes.
    max_display_size: int = 1200


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None


def write_raster_metadata_json(*, raster_path: str, output_json: str) -> None:
    """Write a small JSON blob with CRS/bounds/transform (debug-friendly)."""

    import rasterio

    p = Path(output_json)
    p.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(raster_path) as src:
        transform = src.transform
        bounds = src.bounds
        payload = {
            "path": raster_path,
            "crs": str(src.crs) if src.crs is not None else None,
            "width": int(src.width),
            "height": int(src.height),
            "count": int(src.count),
            "dtype": str(src.dtypes[0]) if src.count else None,
            "nodata": _safe_float(src.nodata),
            "bounds": {
                "left": float(bounds.left),
                "bottom": float(bounds.bottom),
                "right": float(bounds.right),
                "top": float(bounds.top),
            },
            "transform": [
                float(transform.a),
                float(transform.b),
                float(transform.c),
                float(transform.d),
                float(transform.e),
                float(transform.f),
            ],
        }

    p.write_text(json.dumps(payload, indent=2) + "\n")


def render_quicklook(req: QuicklookRequest) -> None:
    """Render a quicklook PNG.

    This tries to use Cartopy for a nice basemap (Natural Earth). If Cartopy is
    not present, it falls back to a plain lon/lat grid with coastlines omitted.

    Notes
    -----
    - We always render in EPSG:4326 for interpretability.
    - We use a robust display stretch (2% / 98% percentiles).
    """

    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds

    # Lazy imports so this module stays usable in minimal envs.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    aoi_bbox = req.aoi_bbox

    Path(req.output_png).parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(req.raster_path) as src:
        src_crs = src.crs
        if src_crs is None:
            raise ValueError(f"Raster {req.raster_path} has no CRS; cannot quicklook")

        # View extent in EPSG:4326
        b = src.bounds
        raster_bounds_4326 = transform_bounds(src_crs, "EPSG:4326", b.left, b.bottom, b.right, b.top, densify_pts=21)

        left, bottom, right, top = raster_bounds_4326
        dx = right - left
        dy = top - bottom
        pad_x = dx * float(req.margin_fraction)
        pad_y = dy * float(req.margin_fraction)
        view = (left - pad_x, bottom - pad_y, right + pad_x, top + pad_y)

        # Read a decimated overview for display; reproject into EPSG:4326
        out_max = int(req.max_display_size)
        scale = max(src.width / out_max, src.height / out_max, 1.0)
        out_w = int(round(src.width / scale))
        out_h = int(round(src.height / scale))

        # Reproject to EPSG:4326 in a coarse grid.
        # We derive the output transform from view bbox.
        from rasterio.transform import from_bounds

        out_transform = from_bounds(view[0], view[1], view[2], view[3], out_w, out_h)

        dst = np.zeros((out_h, out_w), dtype="float32")

        rasterio.warp.reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src_crs,
            dst_transform=out_transform,
            dst_crs="EPSG:4326",
            resampling=Resampling.nearest,
        )

        # Robust stretch
        finite = np.isfinite(dst)
        if finite.any():
            lo, hi = np.nanpercentile(dst[finite], [2, 98])
            if not np.isfinite(lo) or not np.isfinite(hi) or lo == hi:
                lo, hi = float(np.nanmin(dst[finite])), float(np.nanmax(dst[finite]))
        else:
            lo, hi = 0.0, 1.0

    # Build plot
    fig = plt.figure(figsize=(10, 10), dpi=150)

    # Try Cartopy basemap; fall back to normal Axes.
    ax = None
    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature

        ax = plt.axes(projection=ccrs.PlateCarree())
        ax.set_extent([view[0], view[2], view[1], view[3]], crs=ccrs.PlateCarree())
        ax.add_feature(cfeature.LAND.with_scale("50m"), facecolor="#f3f0e8")
        ax.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor="#d6eaf8")
        ax.add_feature(cfeature.COASTLINE.with_scale("50m"), linewidth=0.7)
        ax.add_feature(cfeature.BORDERS.with_scale("50m"), linewidth=0.4, alpha=0.7)
        ax.add_feature(cfeature.LAKES.with_scale("50m"), facecolor="#d6eaf8", alpha=0.7)
        ax.add_feature(cfeature.RIVERS.with_scale("50m"), edgecolor="#2c7fb8", linewidth=0.6, alpha=0.8)
    except Exception as exc:
        logger.info("Cartopy not available for basemap (%s); using plain axes", exc)
        ax = plt.axes()
        ax.set_xlim(view[0], view[2])
        ax.set_ylim(view[1], view[3])
        ax.grid(True, linewidth=0.3, alpha=0.4)

    # Show raster
    extent = [view[0], view[2], view[1], view[3]]
    ax.imshow(dst, extent=extent, origin="upper", cmap="gray", vmin=lo, vmax=hi, alpha=0.75)

    # Draw AOI bbox if provided
    if aoi_bbox is not None and len(aoi_bbox) == 4:
        minx, miny, maxx, maxy = [float(x) for x in aoi_bbox]
        ax.plot([minx, maxx, maxx, minx, minx], [miny, miny, maxy, maxy, miny], color="#e74c3c", linewidth=1.5, label="AOI")

    # Title
    if req.title:
        ax.set_title(req.title)

    # Legend
    try:
        ax.legend(loc="lower left")
    except Exception:
        pass

    fig.tight_layout()
    fig.savefig(req.output_png)
    plt.close(fig)

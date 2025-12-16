"""Raster I/O policies for geoanalytics downloader.

This module exists to pull the highest-complexity code out of
`geoanalytics-downloader.py`:
- opening remote rasters with fsspec
- repairing CRS/transform metadata using STAC proj:* fields
- (optional) Sentinel-1 spatial alignment using a Sentinel-2 reference
- writing GeoTIFF outputs

The goal is to keep downloader orchestration readable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import rasterio

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadToLocalRequest:
    href: str
    local_path: str
    dtype: str | None
    nodata: float | None
    spatial_metadata: Optional[Dict[str, Any]]
    asset_name: Optional[str]
    fallback_crs: Optional[str]
    reference_data: Any = None
    orbit_state: Optional[str] = None
    align_to_reference: bool = False
    clip_bbox: Optional[list[float]] = None
    clip_bbox_crs: str = "EPSG:4326"


class RasterDownloader:
    """Policy object that downloads remote rasters and writes GeoTIFFs locally."""

    def __init__(self, io_client):
        self.io_client = io_client

    def download_to_local(self, request: DownloadToLocalRequest) -> None:
        import fsspec
        import rioxarray as rxr

        reader_opts = self.io_client._storage_options(request.href)

        with fsspec.open(request.href, "rb", **reader_opts) as reader_file:
            with rxr.open_rasterio(reader_file) as dataset:
                label = request.asset_name or request.href

                try:
                    dataset = SpatialMetadataApplier().apply(
                        dataset,
                        request.spatial_metadata,
                        asset_label=label,
                        fallback_crs=request.fallback_crs,
                    )
                except Exception as exc:
                    logger.error("%s: failed applying spatial metadata: %s", label, exc)
                    raise

                try:
                    # Only apply SAR alignment/orientation when explicitly requested.
                    # (Passing xarray objects around can trigger numpy truthiness errors
                    # if any downstream code does `if reference_data:`.)
                    # if request.spatial_metadata is not None:
                    dataset = SarAligner().maybe_align(
                        dataset,
                        spatial_metadata=request.spatial_metadata,
                        reference_data=request.reference_data,
                        orbit_state=request.orbit_state,
                        align_to_reference=request.align_to_reference,
                    )
                except Exception as exc:
                    logger.error("%s: failed SAR alignment step: %s", label, exc)
                    raise

                # Optional per-band clip (saves disk and avoids downstream merge bugs).
                # NOTE: Clip AFTER any SAR alignment/reproject_match so the AOI is
                # applied in the final georegistered grid.
                if request.clip_bbox is not None:
                    try:
                        if dataset.rio.crs is None:
                            logger.warning(
                                "%s: clip requested but dataset CRS is missing; skipping clip",
                                label,
                            )
                        else:
                            from rasterio.warp import transform_bounds

                            minx, miny, maxx, maxy = request.clip_bbox
                            t_minx, t_miny, t_maxx, t_maxy = transform_bounds(
                                request.clip_bbox_crs,
                                dataset.rio.crs,
                                minx,
                                miny,
                                maxx,
                                maxy,
                            )

                            # Intersect with raster bounds to avoid errors.
                            b_left, b_bottom, b_right, b_top = dataset.rio.bounds()
                            i_minx = max(t_minx, b_left)
                            i_miny = max(t_miny, b_bottom)
                            i_maxx = min(t_maxx, b_right)
                            i_maxy = min(t_maxy, b_top)

                            if i_minx >= i_maxx or i_miny >= i_maxy:
                                logger.warning(
                                    "%s: AOI bbox does not intersect raster bounds; skipping clip",
                                    label,
                                )
                            else:
                                dataset = dataset.rio.clip_box(
                                    minx=i_minx,
                                    miny=i_miny,
                                    maxx=i_maxx,
                                    maxy=i_maxy,
                                )
                                try:
                                    dataset.attrs["clipped_to_aoi"] = "true"
                                    dataset.attrs["clip_bbox"] = str(request.clip_bbox)
                                except Exception:
                                    pass
                    except Exception as exc:
                        logger.error("%s: failed clipping to AOI bbox: %s", label, exc)
                        raise

                try:
                    if request.nodata is not None:
                        dataset = dataset.rio.set_nodata(request.nodata)
                        dataset = dataset.rio.write_nodata(request.nodata, encoded=True)
                except Exception as exc:
                    logger.error("%s: failed setting nodata (%s): %s", label, request.nodata, exc)
                    raise

                try:
                    if request.dtype is not None and str(request.dtype).strip() != "":
                        dataset = dataset.astype(request.dtype)
                except Exception as exc:
                    logger.error("%s: failed astype(%s): %s", label, request.dtype, exc)
                    raise

                try:
                    dataset = DimensionNormalizer().normalize(dataset, asset_label=label)
                except Exception as exc:
                    logger.error("%s: failed dimension normalization: %s", label, exc)
                    raise

                profile = {
                    "driver": "GTiff",
                    "dtype": dataset.dtype,
                    "width": dataset.rio.width,
                    "height": dataset.rio.height,
                    "count": dataset.rio.count,
                    "crs": dataset.rio.crs,
                    "transform": dataset.rio.transform(),
                }
                if dataset.rio.nodata is not None:
                    profile["nodata"] = dataset.rio.nodata

                with rasterio.open(request.local_path, "w", **profile) as dst:
                    data = dataset.values
                    if data.ndim == 2:
                        dst.write(data, 1)
                    else:
                        dst.write(data)


class DimensionNormalizer:
    """Ensures rioxarray-compatible dimension ordering.

    rio.to_raster expects DataArray dims in the order ('band', 'y', 'x')
    (or ('y','x') for single-band).
    Some readers can yield ('y','x','band') or ('band','x','y') depending on
    upstream metadata; this normalizer makes the order explicit.
    """

    _BAND_DIM_CANDIDATES = ("band", "bands", "time", "variable")

    def normalize(self, dataset, *, asset_label: str):
        # rioxarray.open_rasterio typically returns a DataArray, but be defensive.
        try:
            import xarray as xr

            if isinstance(dataset, xr.Dataset):
                # If a Dataset slips through, pick the first variable.
                var = next(iter(dataset.data_vars))
                dataset = dataset[var]
        except Exception:
            # xarray might not be importable in some minimal test contexts.
            pass

        dims = list(getattr(dataset, "dims", ()))
        if not dims:
            return dataset

        if "y" not in dims or "x" not in dims:
            # Can't reliably reorder without explicit spatial dims.
            logger.debug(
                "%s: dataset dims missing x/y (%s); leaving as-is", asset_label, dims
            )
            return dataset

        # Determine if a band-like dimension exists.
        band_dim = None
        for candidate in self._BAND_DIM_CANDIDATES:
            if candidate in dims:
                band_dim = candidate
                break

        # Canonicalize to ('band','y','x') where possible.
        if band_dim is None:
            # 2D case or already squeezed; enforce ('y','x') ordering.
            if tuple(dims) != ("y", "x"):
                new_order = [d for d in dims if d not in ("y", "x")] + ["y", "x"]
                logger.debug(
                    "%s: normalizing dims %s -> %s", asset_label, dims, new_order
                )
                return dataset.transpose(*new_order)
            return dataset

        if band_dim != "band":
            # Rename band-like dimension to 'band' for writer compatibility.
            try:
                dataset = dataset.rename({band_dim: "band"})
            except Exception:
                # If rename fails, we can still try to transpose using existing name.
                pass

        dims = list(getattr(dataset, "dims", ()))
        # If after rename we still don't have 'band', locate band dim again.
        effective_band = "band" if "band" in dims else band_dim
        desired = [d for d in dims if d not in (effective_band, "y", "x")]
        desired += [effective_band, "y", "x"]
        if tuple(dims) != tuple(desired):
            logger.debug(
                "%s: normalizing dims %s -> %s", asset_label, dims, desired
            )
            dataset = dataset.transpose(*desired)

        return dataset


class SpatialMetadataApplier:
    """Applies STAC projection metadata to rioxarray datasets."""

    @staticmethod
    def _normalize_grid_orientation(
        dataset,
        spatial_metadata: Dict[str, Any],
        *,
        asset_label: str,
    ):
        """Fix swapped x/y orientation using STAC proj:shape/transform.

        Why this exists:
        - Some Sentinel-1 assets appear rotated 90° in GIS tools when written
          using a transform computed/applied with swapped axis assumptions.
        - xarray/rioxarray uses dims as (..., y, x). We should never assume
          x/y ordering from `dataset.values` alone.

        Strategy:
        - If STAC proj:shape is present and indicates that raster (height,width)
          are swapped, transpose the spatial axes.
        - This is sufficient to resolve the common 90° rotation symptom.

        Returns:
            Possibly transposed dataset.
        """

        stac_shape = spatial_metadata.get("proj:shape")
        if not (isinstance(stac_shape, (list, tuple)) and len(stac_shape) == 2):
            return dataset

        try:
            stac_height, stac_width = int(stac_shape[0]), int(stac_shape[1])
        except Exception:
            return dataset

        height = int(dataset.rio.height)
        width = int(dataset.rio.width)

        if height == stac_height and width == stac_width:
            return dataset

        # If swapped, enforce orientation by transposing spatial axes.
        if height == stac_width and width == stac_height:
            logger.info(
                "%s: Detected swapped raster dims (file h=%s,w=%s vs STAC h=%s,w=%s). Transposing y/x.",
                asset_label,
                height,
                width,
                stac_height,
                stac_width,
            )
            # rioxarray DataArray is typically (band, y, x) or (y, x)
            # Forced transpose of spatial dims only to ensure correct orientation.
            dims = list(getattr(dataset, "dims", ()))
            if "y" in dims and "x" in dims:
                return dataset.transpose(
                    *[d for d in dims if d not in ("y", "x")], "y", "x"
                )

        return dataset

    @staticmethod
    def _is_identity_or_invalid_transform(transform) -> bool:
        if transform is None:
            return True
        from rasterio.transform import Affine

        if not isinstance(transform, Affine):
            return True
        identity = Affine.identity()
        flipped = Affine(1.0, 0.0, 0.0, 0.0, -1.0, 0.0)
        return transform == identity or transform == flipped

    @staticmethod
    def _crs_from_metadata(metadata: Dict[str, Any] | None) -> Optional[str]:
        if not metadata:
            return None

        epsg_value = metadata.get("proj:epsg")
        if epsg_value is not None:
            if isinstance(epsg_value, str):
                epsg_str = epsg_value.strip()
                if epsg_str.upper().startswith("EPSG:"):
                    return epsg_str
                if epsg_str.isdigit():
                    return f"EPSG:{epsg_str}"
                return epsg_str
            return f"EPSG:{int(epsg_value)}"

        proj_code = metadata.get("proj:code")
        if proj_code:
            code_str = str(proj_code).strip()
            if code_str.upper().startswith("EPSG:"):
                return code_str.upper()
            if code_str.isdigit():
                return f"EPSG:{code_str}"
            return code_str

        for key in ("proj:wkt2", "proj:projjson"):
            crs_value = metadata.get(key)
            if crs_value:
                return crs_value

        return None

    @staticmethod
    def _compute_transform_from_bbox(
        metadata: Dict[str, Any], actual_width: int, actual_height: int
    ) -> Optional[tuple[float, ...]]:
        bbox = metadata.get("proj:bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            return None
        if actual_width <= 0 or actual_height <= 0:
            return None

        minx, miny, maxx, maxy = [float(val) for val in bbox]
        xres = (maxx - minx) / float(actual_width)
        yres = (miny - maxy) / float(actual_height)
        return (xres, 0.0, minx, 0.0, yres, maxy)

    def apply(
        self,
        dataset,
        spatial_metadata: Optional[Dict[str, Any]],
        *,
        asset_label: str,
        fallback_crs: Optional[str],
    ):
        current_transform = dataset.rio.transform()
        needs_transform = self._is_identity_or_invalid_transform(current_transform)
        actual_width = dataset.rio.width
        actual_height = dataset.rio.height

        if spatial_metadata:
            # Fix possible swapped x/y orientation first (helps avoid a 90° rotation)
            dataset = self._normalize_grid_orientation(
                dataset, spatial_metadata, asset_label=asset_label
            )

            crs_value = self._crs_from_metadata(spatial_metadata)
            if dataset.rio.crs is None and crs_value:
                dataset = dataset.rio.write_crs(crs_value, inplace=True)

            if needs_transform:
                transform_value = self._compute_transform_from_bbox(
                    spatial_metadata, actual_width, actual_height
                )
                if transform_value:
                    from rasterio.transform import Affine

                    dataset = dataset.rio.write_transform(
                        Affine(*transform_value), inplace=True
                    )
                    needs_transform = False

        if dataset.rio.crs is None and fallback_crs:
            dataset = dataset.rio.write_crs(fallback_crs, inplace=True)

        if dataset.rio.crs is None:
            logger.warning(
                "%s is missing CRS metadata and may fail downstream merges", asset_label
            )
        if needs_transform:
            logger.warning(
                "%s is missing transform metadata; output may not align correctly",
                asset_label,
            )

        return dataset


class SarAligner:
    """Handles optional SAR-to-reference alignment.

    This keeps the downloader readable; the heavy maths can evolve here.

    For now we keep it as a no-op wrapper (it can be expanded incrementally).
    """

    def maybe_align(
        self,
        dataset,
        *,
        spatial_metadata: Dict[str, Any],
        reference_data,
        orbit_state: Optional[str],
        align_to_reference: bool = False,
    ):
        """Optionally apply SAR alignment/orientation fixes.

        Important: `reference_data` may be an xarray object. Never use it (or any
        xarray/numpy array) in a boolean context.
        """

        # If reference_data is our special marker, apply orbit-based orientation
        # correction only (no reproject_match).
        if isinstance(reference_data, str) and reference_data == "__ORBIT_ORIENT__":
            return self._apply_orbit_orientation(dataset, orbit_state=orbit_state)

        # If an xarray reference dataset was provided (e.g., Sentinel-2), we can
        # optionally snap/reproject the SAR grid to match it.
        if align_to_reference and reference_data is not None:
            try:
                # Avoid boolean evaluation of xarray objects.
                ref_crs = getattr(getattr(reference_data, "rio", None), "crs", None)
                if ref_crs is None:
                    logger.warning("SAR align_to_reference requested but reference CRS missing; skipping")
                    return dataset

                # Ensure SAR dataset has a CRS/transform before matching.
                if dataset.rio.crs is None:
                    logger.warning("SAR align_to_reference requested but SAR CRS missing; skipping")
                    return dataset

                # rioxarray will resample; bilinear is typically ok for amplitude-like
                # continuous rasters, but keep nearest as safer default.
                dataset = dataset.rio.reproject_match(reference_data, resampling=rasterio.enums.Resampling.nearest)
                try:
                    dataset.attrs["sar_aligned_to_reference"] = "true"
                except Exception:
                    pass
                return dataset
            except Exception as exc:
                logger.warning("SAR reproject_match failed; leaving as-is: %s", exc)
                return dataset

        _ = (spatial_metadata, orbit_state)
        return dataset

    @staticmethod
    def _apply_orbit_orientation(dataset, *, orbit_state: Optional[str]):
        """Apply a deterministic rotation for Sentinel-1 GRD."""
        if orbit_state is None:
            return dataset

        state = str(orbit_state).strip().lower()
        if state not in {"ascending", "descending"}:
            return dataset

        # 1. Rotate Data (same as before)
        dataset = DimensionNormalizer().normalize(dataset, asset_label="sar")
        data = dataset.values
        k = 1 if state == "descending" else -1

        # Handle 2D vs 3D rotation
        if data.ndim == 2:
            rotated_data = np.rot90(data, k=k, axes=(0, 1))
        else:
            rotated_data = np.rot90(data, k=k, axes=(1, 2))

        # 2. Calculate New Affine Transform (CENTER PRESERVING)
        t = dataset.rio.transform()
        width = dataset.rio.width
        height = dataset.rio.height

        # Calculate the geographic center of the original footprint
        # Note: (width/2, height/2) are pixel coordinates of the center
        center_x, center_y = t * (width / 2, height / 2)

        # New resolutions (swap x/y magnitudes)
        # Assuming North-Up input: t.a > 0, t.e < 0
        new_x_res = abs(t.e)
        new_y_res = -abs(t.a)

        # New dimensions (swapped)
        new_width = height
        new_height = width

        # Calculate New Top-Left (c, f) such that the center point is preserved
        # center_x = new_c + (new_width / 2) * new_x_res
        # center_y = new_f + (new_height / 2) * new_y_res
        new_c = center_x - (new_width / 2) * new_x_res
        new_f = center_y - (new_height / 2) * new_y_res

        new_t = rasterio.transform.Affine(
            new_x_res, 0.0, new_c,
            0.0, new_y_res, new_f
        )

        # 3. Reconstruct DataArray
        # We assume dimensions are simply swapped in name/order context
        # If input was (band, y, x), output is still (band, y, x) but y/x sizes swapped
        try:
            import xarray as xr
            rotated_da = xr.DataArray(
                rotated_data,
                dims=dataset.dims,
                attrs=dataset.attrs,
                name=dataset.name,
            )

            rotated_da = rotated_da.rio.write_crs(dataset.rio.crs, inplace=True)
            rotated_da = rotated_da.rio.write_transform(new_t, inplace=True)

            # Explicitly clear old coords so rioxarray regenerates them from transform
            if "x" in rotated_da.coords: del rotated_da.coords["x"]
            if "y" in rotated_da.coords: del rotated_da.coords["y"]

            if dataset.rio.nodata is not None:
                rotated_da = rotated_da.rio.write_nodata(dataset.rio.nodata, inplace=True)

            return rotated_da

        except Exception:
            return dataset

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
    target_resolution: Optional[float] = None


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
                    if request.orbit_state is not None:
                        # AWS STAC bbox is always EPSG:4326 for S1
                        # Reproject dataset to EPSG:4326 for intersection
                        dataset = dataset.rio.reproject(
                            request.fallback_crs or "EPSG:4326"
                        )
                except Exception as exc:
                    logger.error("%s: failed SAR alignment step: %s", label, exc)
                    raise

                # Optional per-band clip (saves disk and avoids downstream merge bugs).
                # NOTE: Clip AFTER any SAR alignment/reproject_match so the AOI is
                # applied in the final georegistered grid.
                if request.clip_bbox is not None:
                    try:
                        logger.debug(
                            "%s: clip requested. dataset dims=%s shape=%s crs=%s transform=%s bounds=%s",
                            label,
                            getattr(dataset, "dims", None),
                            getattr(dataset, "shape", None),
                            getattr(getattr(dataset, "rio", None), "crs", None),
                            getattr(
                                getattr(dataset, "rio", None), "transform", lambda: None
                            )(),
                            getattr(
                                getattr(dataset, "rio", None), "bounds", lambda: None
                            )(),
                        )
                        if dataset.rio.crs is None:
                            logger.warning(
                                "%s: clip requested but dataset CRS is missing; skipping clip",
                                label,
                            )
                        else:
                            minx, miny, maxx, maxy = request.clip_bbox
                            # Reproject bbox to dataset CRS if needed
                            if request.clip_bbox_crs != str(dataset.rio.crs):
                                from rasterio.warp import transform_bounds

                                minx, miny, maxx, maxy = transform_bounds(
                                    request.clip_bbox_crs,
                                    str(dataset.rio.crs),
                                    minx,
                                    miny,
                                    maxx,
                                    maxy,
                                    densify_pts=21,
                                )
                            dataset = dataset.rio.clip_box(
                                minx=minx, miny=miny, maxx=maxx, maxy=maxy
                            )
                            logger.debug(
                                "%s: clip applied. new dataset dims=%s shape=%s crs=%s transform=%s bounds=%s",
                                label,
                                getattr(dataset, "dims", None),
                                getattr(dataset, "shape", None),
                                getattr(getattr(dataset, "rio", None), "crs", None),
                                getattr(
                                    getattr(dataset, "rio", None),
                                    "transform",
                                    lambda: None,
                                )(),
                                getattr(
                                    getattr(dataset, "rio", None),
                                    "bounds",
                                    lambda: None,
                                )(),
                            )
                    except Exception as exc:
                        logger.error("%s: failed clipping to bbox: %s", label, exc)
                        raise

                try:
                    if request.nodata is not None:
                        dataset = dataset.rio.set_nodata(request.nodata)
                        dataset = dataset.rio.write_nodata(request.nodata, encoded=True)
                except Exception as exc:
                    logger.error(
                        "%s: failed setting nodata (%s): %s", label, request.nodata, exc
                    )
                    raise

                try:
                    if request.dtype is not None and str(request.dtype).strip() != "":
                        dataset = dataset.astype(request.dtype)
                except Exception as exc:
                    logger.error("%s: failed astype(%s): %s", label, request.dtype, exc)
                    raise

                try:
                    dataset = DimensionNormalizer().normalize(
                        dataset, asset_label=label
                    )
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
            logger.debug("%s: normalizing dims %s -> %s", asset_label, dims, desired)
            dataset = dataset.transpose(*desired)

        return dataset


class SpatialMetadataApplier:
    """Applies STAC projection metadata to rioxarray datasets."""

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

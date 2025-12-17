#!/usr/bin/env python3
"""Utility functions for merging and processing raster files in the geoanalytics workflow."""

from __future__ import annotations

import glob
import logging
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import fsspec
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.merge import merge
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles

logger = logging.getLogger(__name__)


def reproject_raster_dataset(
    src: rasterio.DatasetReader, dst_crs
) -> rasterio.DatasetReader:
    """Reproject a raster dataset to a target CRS, returning an in-memory dataset."""
    src_crs = src.crs
    transform, width, height = calculate_default_transform(
        src_crs, dst_crs, src.width, src.height, *src.bounds
    )
    kwargs = src.meta.copy()
    kwargs.update(
        {"crs": dst_crs, "transform": transform, "width": width, "height": height}
    )
    memfile = MemoryFile()
    with memfile.open(**kwargs) as dst:
        for i in range(1, src.count + 1):
            reproject(
                source=rasterio.band(src, i),
                destination=rasterio.band(dst, i),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.nearest,
            )
    return memfile.open()


def mosaic_tifs(
    tif_files: List[str], dst_crs=None
) -> Tuple[np.ndarray, rasterio.Affine, Any]:
    """
    Mosaic multiple TIF files into a single array.

    Args:
        tif_files: List of paths to TIF files to mosaic.
        dst_crs: Target CRS for the output. If None, uses the CRS of the largest file.

    Returns:
        Tuple of (mosaic array, transform, CRS).
    """
    tif_files = sorted(tif_files, key=lambda x: os.path.getsize(x))
    src_files_to_mosaic = []
    dst_crs_ret = dst_crs

    for tif in reversed(tif_files):
        src = rasterio.open(tif, "r")
        crs = src.meta["crs"]
        if dst_crs_ret is None:
            dst_crs_ret = crs
            src_files_to_mosaic.append(src)
            continue
        if crs == dst_crs_ret:
            src_files_to_mosaic.append(src)
            continue
        reproj_src = reproject_raster_dataset(src, dst_crs=dst_crs_ret)
        src_files_to_mosaic.append(reproj_src)

    mosaic, out_trans = merge(src_files_to_mosaic)
    return mosaic, out_trans, dst_crs_ret


def stack_bands(
    tif_files: List[str],
    dst_crs=None,
    target_resolution: Optional[float] = None,
    clip_bbox: Optional[List[float]] = None,
    clip_bbox_crs: str = "EPSG:4326",
) -> Tuple[np.ndarray, rasterio.Affine, Any, Dict[str, Any]]:
    """
    Stack multiple single-band TIF files into a single multi-band array.

    Unlike mosaic_tifs which spatially merges tiles, this function stacks
    individual band files (e.g., B02.tif, B03.tif, B04.tif) into a single
    multi-band array.

    All bands are resampled/reprojected to match the grid of the first
    (reference) band.

    Args:
        tif_files: List of paths to single-band TIF files to stack.
        dst_crs: Target CRS. If None, uses CRS from the first file.
        target_resolution: Target resolution in CRS units. If None, uses
                          the resolution of the first file.
        clip_bbox: Optional bounding box [minx, miny, maxx, maxy] to clip output to.
        clip_bbox_crs: CRS of the clip_bbox (default: EPSG:4326 / WGS84).

    Returns:
        Tuple of (stacked array, transform, CRS, metadata dict).
        The stacked array has shape (num_bands, height, width).
    """
    from rasterio.warp import transform_bounds

    if not tif_files:
        raise ValueError("No TIF files provided for stacking")

    # Use the first file as the reference for grid alignment
    with rasterio.open(tif_files[0], "r") as ref_src:
        # ref_crs is the *output* CRS we will stack into.
        # If dst_crs is provided, we will reproject all bands to it.
        ref_crs = dst_crs or ref_src.crs
        ref_transform = ref_src.transform
        ref_width = ref_src.width
        ref_height = ref_src.height
        ref_bounds = ref_src.bounds
        ref_dtype = ref_src.dtypes[0]
        ref_nodata = ref_src.nodata

        # If we are changing output CRS or resolution (even without clipping),
        # compute the output grid now.
        if target_resolution is not None or (
            dst_crs is not None and dst_crs != ref_src.crs
        ):
            res = target_resolution if target_resolution else abs(ref_transform[0])
            ref_transform, ref_width, ref_height = calculate_default_transform(
                ref_src.crs,
                ref_crs,
                ref_src.width,
                ref_src.height,
                *ref_bounds,
                resolution=res,
            )

    # If clip_bbox is provided, transform it to the *output* CRS (ref_crs) and
    # intersect with the raster bounds.
    #
    # Important: if dst_crs != ref_src.crs (e.g., target_crs=auto_utm), we must
    # NOT transform AOI bbox into ref_src.crs; we must transform into ref_crs,
    # otherwise clipping is applied in the wrong coordinate space.
    if clip_bbox is not None:
        minx, miny, maxx, maxy = clip_bbox

        # Transform clip_bbox from its CRS to the output CRS
        try:
            transformed_bbox = transform_bounds(
                clip_bbox_crs, ref_crs, minx, miny, maxx, maxy
            )
            t_minx, t_miny, t_maxx, t_maxy = transformed_bbox
            logger.info(
                f"Transformed clip bbox from {clip_bbox_crs} to {ref_crs}: "
                f"[{t_minx:.2f}, {t_miny:.2f}, {t_maxx:.2f}, {t_maxy:.2f}]"
            )
        except Exception as e:
            logger.warning(f"Failed to transform clip bbox: {e}, using as-is")
            t_minx, t_miny, t_maxx, t_maxy = minx, miny, maxx, maxy

            # Intersect transformed clip_bbox with raster bounds
            # IMPORTANT: ref_bounds are expressed in ref_src.crs. If the output
            # CRS differs (dst_crs/ref_crs), compare in the output CRS.
            bounds_left = ref_bounds.left
            bounds_bottom = ref_bounds.bottom
            bounds_right = ref_bounds.right
            bounds_top = ref_bounds.top

            if (
                ref_src.crs is not None
                and ref_crs is not None
                and ref_src.crs != ref_crs
            ):
                try:
                    b_left, b_bottom, b_right, b_top = transform_bounds(
                        ref_src.crs,
                        ref_crs,
                        bounds_left,
                        bounds_bottom,
                        bounds_right,
                        bounds_top,
                    )
                    bounds_left, bounds_bottom, bounds_right, bounds_top = (
                        b_left,
                        b_bottom,
                        b_right,
                        b_top,
                    )
                    logger.info(
                        f"Transformed raster bounds from {ref_src.crs} to {ref_crs}: "
                        f"[{bounds_left:.2f}, {bounds_bottom:.2f}, {bounds_right:.2f}, {bounds_top:.2f}]"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to transform raster bounds from {ref_src.crs} to {ref_crs}: {e}"
                    )

            clipped_minx = max(t_minx, bounds_left)
            clipped_miny = max(t_miny, bounds_bottom)
            clipped_maxx = min(t_maxx, bounds_right)
            clipped_maxy = min(t_maxy, bounds_top)

            if clipped_minx >= clipped_maxx or clipped_miny >= clipped_maxy:
                logger.warning(
                    f"Clip bbox [{t_minx:.2f}, {t_miny:.2f}, {t_maxx:.2f}, {t_maxy:.2f}] "
                    f"does not intersect raster bounds [{bounds_left:.2f}, {bounds_bottom:.2f}, "
                    f"{bounds_right:.2f}, {bounds_top:.2f}] in {ref_crs}. Skipping clip."
                )
            else:
                ref_bounds = rasterio.coords.BoundingBox(
                    clipped_minx, clipped_miny, clipped_maxx, clipped_maxy
                )
                logger.info(
                    f"Clipping to AOI bounds: [{clipped_minx:.2f}, {clipped_miny:.2f}, "
                    f"{clipped_maxx:.2f}, {clipped_maxy:.2f}]"
                )

    # If target resolution is specified or clipping, recalculate grid dimensions
    # in the output CRS.
    if clip_bbox is not None:
        res = target_resolution if target_resolution else abs(ref_transform[0])
        ref_transform, ref_width, ref_height = calculate_default_transform(
            ref_src.crs,
            ref_crs,
            ref_src.width,
            ref_src.height,
            *ref_bounds,
            resolution=res,
        )

    # Pre-allocate the output array
    num_bands = len(tif_files)
    stacked = np.zeros((num_bands, ref_height, ref_width), dtype=ref_dtype)

    # Read and resample each band
    for i, tif_path in enumerate(tif_files):
        with rasterio.open(tif_path, "r") as src:
            # Check if reprojection/resampling is needed
            needs_reproject = (
                src.crs != ref_crs
                or src.transform != ref_transform
                or src.width != ref_width
                or src.height != ref_height
            )

            if needs_reproject:
                # Reproject/resample to match reference grid
                reproject(
                    source=rasterio.band(src, 1),
                    destination=stacked[i],
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=ref_transform,
                    dst_crs=ref_crs,
                    resampling=Resampling.bilinear,
                )
            else:
                # Direct read
                stacked[i] = src.read(1)

    metadata = {
        "dtype": ref_dtype,
        "nodata": ref_nodata,
        "bounds": ref_bounds,
    }

    return stacked, ref_transform, ref_crs, metadata


def merge_tifs(
    tif_files: List[str],
    out_file: str,
    descriptions: str,
    descriptions_meta: str,
    bandnames: Optional[List[str]] = None,
    dst_crs=None,
    RGB: bool = False,
    min_max: Tuple[Optional[float], Optional[float]] = (None, None),
    **extra_info,
) -> Tuple[int, Any]:
    """
    Merge multiple TIF files into a single output file.

    Args:
        tif_files: List of paths to TIF files to merge.
        out_file: Output file path.
        descriptions: Description string to embed in the output.
        descriptions_meta: Metadata description to embed.
        bandnames: Optional list of band names for the output.
        dst_crs: Target CRS. If None, uses CRS from input files.
        RGB: If True, scales output to 0-255 uint8 for RGB visualization.
        min_max: Tuple of (min, max) values for RGB scaling.
        **extra_info: Additional metadata tags (e.g., cloud_percentage).

    Returns:
        Tuple of (success code, output CRS).
    """
    tif_files = sorted(tif_files, key=lambda x: os.path.getsize(x))
    if bandnames is not None:
        bandnames_c = bandnames.copy()
    else:
        bandnames_c = None

    src_files_to_mosaic = []
    dst_crs_ret = dst_crs

    for tif in reversed(tif_files):
        src = rasterio.open(tif, "r")
        crs = src.meta["crs"]
        if dst_crs_ret is None:
            dst_crs_ret = crs
            src_files_to_mosaic.append(src)
            continue
        if crs == dst_crs_ret:
            src_files_to_mosaic.append(src)
            continue
        reproj_src = reproject_raster_dataset(src, dst_crs=dst_crs_ret)
        src_files_to_mosaic.append(reproj_src)

    mosaic, out_trans = merge(src_files_to_mosaic)
    out_meta = src.meta.copy()

    if RGB:
        invalid_mask = mosaic == 0
        mosaic = (np.clip(mosaic, min_max[0], min_max[1]) / min_max[1]) * 255
        mosaic[invalid_mask] = 0
        mosaic = mosaic.astype("uint8")
        out_meta["dtype"] = rasterio.uint8

    out_meta.update(
        {
            "driver": "GTiff",
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": out_trans,
            "count": mosaic.shape[0],
            "crs": dst_crs_ret,
        }
    )

    with rasterio.open(out_file, "w", **out_meta) as dst:
        dst.update_tags(info=descriptions)
        dst.update_tags(info_item=descriptions_meta)

        for key in extra_info:
            if key == "cloud_percentage":
                dst.update_tags(cloud_percentage=extra_info[key])

        dst.write(mosaic)
        if bandnames_c is not None:
            dst.descriptions = tuple(bandnames_c)

    return 1, dst_crs_ret


class DownloadDirIncompleteError(Exception):
    """Raised when a download directory does not contain valid TIF files."""

    def __init__(self, download_dir: str):
        self.download_dir = download_dir
        super().__init__(f"Download directory incomplete or empty: {download_dir}")


def merge_download_dir(
    download_dir: str,
    output_path: str,
    descriptions_meta: str,
    descriptions: List[str],
    dst_crs=None,
    bandnames: Optional[List[str]] = None,
    remove_temp: bool = True,
    RGB: bool = False,
    min_max: Tuple[Optional[float], Optional[float]] = (None, None),
    min_file_size_kb: float = 20.0,
    **extra_info,
) -> Any:
    """
    Merge all TIF files in a download directory into a single output file.

    This is designed for merging multiple downloaded tiles/chunks into a
    single cohesive raster file.

    Args:
        download_dir: Directory containing the downloaded TIF files.
        output_path: Path for the merged output file.
        descriptions_meta: Metadata description string.
        descriptions: List of description strings to join.
        dst_crs: Target CRS. If None, uses CRS from input files.
        bandnames: Optional list of band names.
        remove_temp: If True, removes the download directory after merge.
        RGB: If True, scales to uint8 RGB.
        min_max: Min/max values for RGB scaling.
        min_file_size_kb: Minimum file size in KB to consider valid.
        **extra_info: Additional metadata tags.

    Returns:
        The output CRS used.

    Raises:
        DownloadDirIncompleteError: If no valid TIF files found.
    """
    # Find TIF files matching the expected pattern and above minimum size
    tifs = [
        f
        for f in glob.glob(os.path.join(download_dir, "*.tif"))
        if (os.path.getsize(f) / 1024.0) > min_file_size_kb
    ]

    if len(tifs) < 1:
        raise DownloadDirIncompleteError(download_dir)

    ret, dst_crs = merge_tifs(
        tifs,
        output_path,
        descriptions=":".join(descriptions)
        if isinstance(descriptions, list)
        else descriptions,
        descriptions_meta=descriptions_meta,
        bandnames=bandnames,
        dst_crs=dst_crs,
        RGB=RGB,
        min_max=min_max,
        **extra_info,
    )

    if ret == 1 and remove_temp:
        shutil.rmtree(download_dir)

    return dst_crs


def merge_downloaded_assets_to_cog(
    tif_files: List[str],
    output_path: str,
    io_client,
    bandnames: Optional[List[str]] = None,
    descriptions: Optional[str] = None,
    dst_crs=None,
    compression: str = "deflate",
    remove_temp: bool = True,
    clip_bbox: Optional[List[float]] = None,
    target_resolution: Optional[float] = None,
    **extra_tags,
) -> str:
    """
    Stack multiple downloaded single-band TIF files and write as a Cloud Optimized GeoTIFF (COG).

    This function is designed for the geoanalytics workflow where individual band
    files are downloaded from remote sources and need to be stacked into a single
    multi-band COG for storage.

    Args:
        tif_files: List of local paths to single-band TIF files to stack.
        output_path: Target path (can be local, abfs://, s3://, etc.).
        io_client: GeoanalyticsIOClient instance for writing to cloud storage.
        bandnames: Optional list of band names for the output.
        descriptions: Optional description string to embed.
        dst_crs: Target CRS. If None, uses CRS from input files.
        compression: COG compression method (default: deflate).
        remove_temp: If True, removes source TIF files after successful merge.
        clip_bbox: Optional bounding box [minx, miny, maxx, maxy] to clip output to.
    target_resolution: Optional pixel size (in CRS units) to resample all bands to.
        **extra_tags: Additional metadata tags to write.

    Returns:
        The output path where the file was written.
    """
    if not tif_files:
        raise ValueError("No TIF files provided for stacking")

    # Filter out files that are too small (likely corrupt or empty)
    valid_tifs = [f for f in tif_files if os.path.getsize(f) > 20 * 1024]
    if not valid_tifs:
        raise DownloadDirIncompleteError("No valid TIF files found (all below 20KB)")

    try:
        # Stack bands into a single array
        stacked, out_trans, dst_crs_ret, metadata = stack_bands(
            valid_tifs,
            dst_crs=dst_crs,
            target_resolution=target_resolution,
            clip_bbox=clip_bbox,
        )

        # Build output metadata
        out_meta = {
            "driver": "GTiff",
            "height": stacked.shape[1],
            "width": stacked.shape[2],
            "transform": out_trans,
            "count": stacked.shape[0],
            "crs": dst_crs_ret,
            "dtype": stacked.dtype,
        }
        if metadata.get("nodata") is not None:
            out_meta["nodata"] = metadata["nodata"]

        # Write to a temporary file first, then convert to COG
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp_src:
            tmp_src_path = tmp_src.name

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp_cog:
            tmp_cog_path = tmp_cog.name

        try:
            # Write the stacked raster
            with rasterio.open(tmp_src_path, "w", **out_meta) as dst:
                if descriptions:
                    dst.update_tags(info=descriptions)
                for key, value in extra_tags.items():
                    dst.update_tags(**{key: value})

                # Minimal QA metadata to aid debugging misalignment issues.
                try:
                    dst.update_tags(output_crs=str(dst_crs_ret))
                    dst.update_tags(output_bounds=str(metadata.get("bounds")))
                    dst.update_tags(output_transform=str(out_trans))
                except Exception:
                    pass

                dst.write(stacked)
                if bandnames:
                    dst.descriptions = tuple(bandnames[: stacked.shape[0]])
                if clip_bbox is not None:
                    dst.update_tags(clipped_to_aoi="true")
                    dst.update_tags(clip_bbox=str(clip_bbox))

            # Convert to COG
            cog_profile = cog_profiles.get(compression)
            cog_profile.update(
                {
                    "blockxsize": 256,
                    "blockysize": 256,
                }
            )

            config = {
                "GDAL_NUM_THREADS": "ALL_CPUS",
                "GDAL_TIFF_INTERNAL_MASK": True,
                "GDAL_TIFF_OVR_BLOCKSIZE": "128",
            }

            cog_translate(
                tmp_src_path,
                tmp_cog_path,
                cog_profile,
                config=config,
                in_memory=False,
            )

            # Write to output (handles cloud storage via io_client)
            writer_opts = io_client._storage_options(output_path, write=True)
            with open(tmp_cog_path, "rb") as src_file:
                with fsspec.open(
                    output_path, "wb", auto_mkdir=True, **writer_opts
                ) as dest_file:
                    shutil.copyfileobj(src_file, dest_file)

            logger.info(f"Successfully wrote stacked COG to {output_path}")

        finally:
            # Clean up temp files
            if os.path.exists(tmp_src_path):
                os.unlink(tmp_src_path)
            if os.path.exists(tmp_cog_path):
                os.unlink(tmp_cog_path)

        # Optionally remove source files
        if remove_temp:
            for tif in tif_files:
                if os.path.exists(tif):
                    os.unlink(tif)

        return output_path

    except Exception as e:
        logger.error(f"Failed to stack and write COG: {e}")
        raise


def collect_downloaded_tifs(
    download_dir: str, pattern: str = "*.tif", min_size_kb: float = 20.0
) -> List[str]:
    """
    Collect valid TIF files from a download directory.

    Args:
        download_dir: Directory to search.
        pattern: Glob pattern for matching files.
        min_size_kb: Minimum file size in KB.

    Returns:
        List of paths to valid TIF files, sorted by size (largest first).
    """
    all_tifs = glob.glob(os.path.join(download_dir, pattern))
    valid_tifs = [f for f in all_tifs if os.path.getsize(f) > min_size_kb * 1024]
    return sorted(valid_tifs, key=lambda x: os.path.getsize(x), reverse=True)
    return sorted(valid_tifs, key=lambda x: os.path.getsize(x), reverse=True)

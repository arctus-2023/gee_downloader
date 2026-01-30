#!/usr/bin/env python3
"""I/O helpers for Geoanalytics downloads that know about ADLS/S3."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import Any, List, Optional

import fsspec
from azure.storage.blob import ContentSettings
from fsspec.core import split_protocol
from shapely.geometry import box, shape

try:
    from azure.identity.aio import DefaultAzureCredential
except ImportError:
    DefaultAzureCredential = None


@dataclass(frozen=True)
class IOConfig:
    """Configuration for the Geoanalytics I/O layer."""

    adl_account: Optional[str] = None
    adl_credential: Optional[Any] = None


class GeoanalyticsIOClient:
    """Encapsulates read/write semantics between S3, ADLS, and local storage."""

    def __init__(self, config: IOConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)

    def close(self) -> None:
        pass

    def load_remote_aoi(self, path: str) -> List[float]:
        """Load a remote AOI file to get bounding box."""
        # Verify remote source path is prefixes with abfs or az and exists
        reader_opts = self._storage_options(path)
        with fsspec.open(path, "r", **reader_opts) as fh:
            import json

            data = json.load(fh)
        features = []
        if "features" in data:
            features = data["features"]
        elif "geometry" in data:
            features = [data]
        else:
            raise ValueError("Invalid AOI file format")
        if not features:
            raise ValueError("No features found in AOI file")
        # Compute bounding box
        minx, miny, maxx, maxy = None, None, None, None
        for feature in features:
            geom = shape(feature["geometry"])
            bbox = box(*geom.bounds)
            if minx is None:
                minx, miny, maxx, maxy = bbox.bounds
            else:
                minx = min(minx, bbox.bounds[0])
                miny = min(miny, bbox.bounds[1])
                maxx = max(maxx, bbox.bounds[2])
                maxy = max(maxy, bbox.bounds[3])
        if minx is None:
            raise ValueError("Could not compute bounding box from AOI features")
        return [minx, miny, maxx, maxy]

    def submit_copy(
        self,
        src: str,
        dest: str,
        dtype: str,
        nodata: float | None,
        clip_bbox: Optional[List[float]] = None,
    ) -> Optional[Any]:
        """Copy a remote asset.

        Args:
            clip_bbox: Optional bounding box [minx, miny, maxx, maxy] to clip the raster to.
        """
        if src.lower().endswith((".jp2", ".jpx", ".jpeg2000", ".tif", ".tiff")):
            self.copy_asset_as_cog(src, dest, dtype, nodata, clip_bbox)
        else:
            self.copy_asset(src, dest)
        return None

    def copy_asset(self, src: str, dest: str) -> None:
        """Copy an asset between two filesystems while honoring protocol-specific auth."""
        reader_opts = self._storage_options(src)
        writer_opts = self._storage_options(dest, write=True)
        with fsspec.open(src, "rb", **reader_opts) as reader:
            with fsspec.open(dest, "wb", auto_mkdir=True, **writer_opts) as writer:
                shutil.copyfileobj(reader, writer)

    def copy_asset_as_cog(
        self,
        src: str,
        dest: str,
        dtype: str,
        nodata: float | None,
        clip_bbox: Optional[List[float]] = None,
    ) -> None:
        """Convert a raster file to a Cloud Optimized GeoTIFF (COG).

        Args:
            clip_bbox: Optional bounding box [minx, miny, maxx, maxy] to clip the raster to.
        """

        import rioxarray as rxr

        # Update destination suffix to .tif
        dest = dest.replace(".jp2", ".tif")
        dest = dest.replace(".jpx", ".tif")
        dest = dest.replace(".jpeg2000", ".tif")
        dest = dest.replace(".tiff", ".tif")

        reader_opts = self._storage_options(src)
        writer_opts = self._storage_options(dest, write=True)
        with fsspec.open(src, "rb", **reader_opts) as reader_file:
            import numpy as np  # noqa: F401
            import rasterio  # noqa: F401
            from rio_cogeo.cogeo import cog_translate
            from rio_cogeo.profiles import cog_profiles

            with rxr.open_rasterio(reader_file) as dataset:
                # rioxarray and XArray sometimes misinterpret nodata values
                if nodata is not None:
                    dataset = dataset.rio.set_nodata(nodata)
                    dataset = dataset.rio.write_nodata(nodata, encoded=True)
                # rioxarray and XArray typically misinterprets dtypes and force float64
                if dtype:
                    dataset = dataset.astype(dtype)
                writer_opts.update(
                    {
                        "content_settings": ContentSettings(
                            content_type="image/tiff; application=geotiff",
                            content_encoding="zstd",
                        )
                    }
                )
                config = {
                    "GDAL_NUM_THREADS": "ALL_CPUS",
                    "GDAL_TIFF_INTERNAL_MASK": True,
                    "GDAL_TIFF_OVR_BLOCKSIZE": "128",
                    "OVERVIEW_COUNT": "16",
                    "OVERVIEW_COMPRESS": "DEFLATE",
                }

                # Clip to AOI bounding box if provided (assumes bbox is in EPSG:4326)
                if clip_bbox is not None:
                    from rasterio.warp import transform_bounds

                    minx, miny, maxx, maxy = clip_bbox

                    # Transform clip_bbox from WGS84 to the dataset's CRS
                    if dataset.rio.crs is not None:
                        try:
                            t_minx, t_miny, t_maxx, t_maxy = transform_bounds(
                                "EPSG:4326", dataset.rio.crs, minx, miny, maxx, maxy
                            )
                            print(
                                f"  Transformed clip bbox to {dataset.rio.crs}: "
                                f"[{t_minx:.2f}, {t_miny:.2f}, {t_maxx:.2f}, {t_maxy:.2f}]"
                            )
                        except Exception as e:
                            print(f"  Warning: Failed to transform clip bbox: {e}")
                            t_minx, t_miny, t_maxx, t_maxy = minx, miny, maxx, maxy
                    else:
                        t_minx, t_miny, t_maxx, t_maxy = minx, miny, maxx, maxy

                    try:
                        dataset = dataset.rio.clip_box(
                            minx=t_minx, miny=t_miny, maxx=t_maxx, maxy=t_maxy
                        )
                        print(f"  Clipped dataset bounds: {dataset.rio.bounds()}")
                    except Exception as e:
                        print(
                            f"  Warning: Failed to clip to AOI: {e}, using full extent"
                        )

                with rasterio.MemoryFile() as tmp_cog_file_src:
                    print("writing raster to memory file")
                    dataset.rio.to_raster(tmp_cog_file_src.name)
                    with rasterio.MemoryFile() as tmp_cog_file_dst:
                        print("writing cog from raster to new memory file")
                        cog_profile = cog_profiles.get("deflate")
                        cog_profile.update(
                            {
                                "blockxsize": 128,
                                "blockysize": 128,
                            }
                        )
                        if dtype:
                            cog_profile["dtype"] = dtype
                        if nodata is not None:
                            cog_profile["nodata"] = nodata
                        cog_translate(
                            tmp_cog_file_src.name,
                            tmp_cog_file_dst.name,
                            cog_profile,
                            config=config,
                            in_memory=True,
                        )
                        print("writing cog dataset to blob storage")
                        with fsspec.open(
                            dest, "wb", auto_mkdir=True, **writer_opts
                        ) as writer:
                            writer.write(tmp_cog_file_dst.read())

                # with rasterio.io.MemoryFile() as tmp_cog_file:
                #     with tmp_cog_file.open(**cog_profile) as cog_dataset:
                #         cog_dataset_name = cog_dataset.name
                #         dataset.rio.to_raster(cog_dataset_name, driver="COG")
                #     # tmp_cog_file.seek(0)
                #     with fsspec.open(
                #         dest, "wb", auto_mkdir=True, **writer_opts
                #     ) as writer:
                #         writer.write(tmp_cog_file.read())

    def _storage_options(self, path: str, write: bool = False) -> dict[str, Any]:
        protocol, _ = split_protocol(path)
        if isinstance(protocol, list):
            protocol = protocol[0]
        options: dict[str, Any] = {}
        if not protocol:
            # Local file - create parent directory if writing
            if write and "/" in path:
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            return options
        scheme = str(protocol).lower()
        if scheme == "s3":
            options["anon"] = True
        elif scheme.startswith("abfs"):
            account = self.config.adl_account or os.environ.get(
                "GA_STORAGE_ACCOUNT_NAME"
            )
            if account:
                options["account_name"] = account
            credential = self.config.adl_credential
            if credential is None and DefaultAzureCredential is not None:
                credential = DefaultAzureCredential()
            if credential is not None:
                options["credential"] = credential
        elif scheme in {"file", "https", "http"} and write and "/" in path:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        return options

    def load_remote_aoi_gdf(self, remote_path: str) -> gpd.GeoDataFrame:
        """Load a GeoJSON or shapefile from cloud storage into a GeoDataFrame."""
        import fsspec
        import geopandas as gpd

        storage_opts = self._storage_options(remote_path)
        with fsspec.open(remote_path, "rb", **storage_opts) as f:
            return gpd.read_file(f)
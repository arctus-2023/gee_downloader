#!/usr/bin/env python3
"""I/O helpers for Geoanalytics downloads that know about ADLS/S3."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import Any, Optional

import fsspec
from azure.storage.blob import ContentSettings
from fsspec.core import split_protocol

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

    def submit_copy(
        self, src: str, dest: str, dtype: str, nodata: float | None
    ) -> Optional[Any]:
        """Copy a remote asset."""
        if src.lower().endswith((".jp2", ".jpx", ".jpeg2000", ".tif", ".tiff")):
            self.copy_asset_as_cog(src, dest, dtype, nodata)
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
        self, src: str, dest: str, dtype: str, nodata: float | None
    ) -> None:
        """Convert a raster file to a Cloud Optimized GeoTIFF (COG)."""

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
            import rioxarray  # noqa: F401
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

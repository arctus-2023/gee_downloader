"""Merge/ingest strategies for downloaded raster assets.

This file introduces explicit policy objects for the downloader output modes.

Currently we only support merged Cloud Optimized GeoTIFF (COG) output.

The current downloader already has utility functions in `utils.py`.
These strategy objects provide a cleaner surface for the orchestration layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Protocol

from geoanalytics_io_client import GeoanalyticsIOClient
from utils import (
    merge_downloaded_assets_to_cog,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeContext:
    section: str
    date_str: str
    item_id: str
    aoi_name: str
    resolution: int
    clip_bbox: Optional[List[float]]
    cloud_cover: Optional[float]
    target_crs: Optional[str]
    target_resolution: Optional[float]


class MergeStrategy(Protocol):
    def merge(
        self,
        *,
        tif_files: List[str],
        bandnames: List[str],
        output_path: str,
        io_client: GeoanalyticsIOClient,
        context: MergeContext,
        chunks: tuple[int, int, int],
    ) -> None: ...


class CogMergeStrategy:
    def merge(
        self,
        *,
        tif_files: List[str],
        bandnames: List[str],
        output_path: str,
        io_client: GeoanalyticsIOClient,
        context: MergeContext,
        chunks: tuple[int, int, int],
    ) -> None:
        # `chunks` is kept for API compatibility with older merge strategies.
        _ = chunks
        descriptions = f"{context.section}:{context.date_str}:{context.item_id}"
        merge_downloaded_assets_to_cog(
            tif_files=tif_files,
            output_path=output_path,
            io_client=io_client,
            bandnames=bandnames,
            descriptions=descriptions,
            dst_crs=context.target_crs,
            target_resolution=context.target_resolution,
            remove_temp=False,
            cloud_percentage=context.cloud_cover,
            clip_bbox=context.clip_bbox,
        )


def build_merge_strategy(
    *,
    output_format: str,
    hierarchical: bool,
    hierarchical_spec: Optional[object] = None,
) -> MergeStrategy:
    # Hierarchical and spec are legacy Zarr knobs; ignored now.
    _ = hierarchical
    _ = hierarchical_spec
    if output_format == "cog":
        return CogMergeStrategy()
    raise ValueError(f"Unsupported output_format: {output_format}")

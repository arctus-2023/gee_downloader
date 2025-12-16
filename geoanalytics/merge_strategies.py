"""Merge/ingest strategies for downloaded raster assets.

This file introduces explicit policy objects for the different output modes:
- COG merge (single GeoTIFF upload)
- flat Zarr merge (single zarr store per scene)
- hierarchical Zarr ingest (single store with groups per scene)

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
    merge_downloaded_assets_to_zarr,
    open_or_create_zarr_store,
    write_merged_to_zarr_group,
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


class FlatZarrMergeStrategy:
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
        descriptions = f"{context.section}:{context.date_str}:{context.item_id}"
        merge_downloaded_assets_to_zarr(
            tif_files=tif_files,
            output_path=output_path,
            io_client=io_client,
            bandnames=bandnames,
            descriptions=descriptions,
            chunks=chunks,
            remove_temp=False,
            dst_crs=context.target_crs,
            target_resolution=context.target_resolution,
            cloud_percentage=context.cloud_cover,
            clip_bbox=context.clip_bbox,
        )


@dataclass(frozen=True)
class HierarchicalZarrSpec:
    store_path: str
    group_path: str


class HierarchicalZarrIngestStrategy:
    def __init__(self, spec: HierarchicalZarrSpec):
        self.spec = spec

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
        # output_path is ignored; we ingest into (store_path, group_path)
        logger.info("Opening/creating Zarr store: %s", self.spec.store_path)
        open_or_create_zarr_store(self.spec.store_path, io_client, mode="a")

        write_merged_to_zarr_group(
            tif_files=tif_files,
            store_path=self.spec.store_path,
            group_path=self.spec.group_path,
            io_client=io_client,
            bandnames=bandnames,
            chunks=chunks,
            clip_bbox=context.clip_bbox,
            dst_crs=context.target_crs,
            target_resolution=context.target_resolution,
            stac_item_id=context.item_id,
            date=context.date_str,
            cloud_cover=context.cloud_cover,
            aoi=context.aoi_name,
            resolution=context.resolution,
        )


def build_merge_strategy(
    *,
    output_format: str,
    hierarchical: bool,
    hierarchical_spec: Optional[HierarchicalZarrSpec] = None,
) -> MergeStrategy:
    if output_format == "cog":
        return CogMergeStrategy()
    if output_format == "zarr" and hierarchical:
        if hierarchical_spec is None:
            raise ValueError("hierarchical_spec is required for hierarchical zarr")
        return HierarchicalZarrIngestStrategy(hierarchical_spec)
    if output_format == "zarr":
        return FlatZarrMergeStrategy()
    raise ValueError(f"Unsupported output_format: {output_format}")

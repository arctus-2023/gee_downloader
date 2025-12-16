#!/usr/bin/env python3
"""Download Earth observation scenes from AWS or Planetary Computer STAC endpoints."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import adlfs  # registers the `abfs` protocol for fsspec  # noqa: F401
import pendulum
from geoanalytics_io_client import GeoanalyticsIOClient, IOConfig
from pystac_client import Client
from utils import (
    open_or_create_zarr_store,
    write_band_to_zarr_group,
)

from asset_policy import (
    AssetDownloadPolicy,
    build_asset_alias_map,
    normalize_band_name as _normalize_band_name_impl,
    safe_split as _safe_split_impl,
)

from stac_search import StacSearchConfig, StacSearcher

from merge_strategies import (
    MergeContext,
    build_merge_strategy,
    HierarchicalZarrSpec,
)

from raster_io import DownloadToLocalRequest, RasterDownloader
from qa_quicklook import QuicklookRequest, render_quicklook, write_raster_metadata_json

logger = logging.getLogger(__name__)

STAC_ENDPOINTS = [
    "https://earth-search.aws.element84.com/v1",
    "https://planetarycomputer.microsoft.com/api/stac/v1",
]

STAC_COLLECTION_MAP = {
    "LC08_L1TOA": "landsat-8-l1",
    "LC08_L2RGB": "landsat-8-l2",
    "S2_L1TOA": "sentinel-2-l1c",
    "S2_L2RGB": "sentinel-2-l2a",
    "S2_L2SURF": "sentinel-2-l2a",
    "S1_L1C": "sentinel-1-grd",
}

EARTH_SEARCH_ASSET_MAP = {
    "S2_L1TOA": {
        "B01": "coastal",
        "B02": "blue",
        "B03": "green",
        "B04": "red",
        "B05": "rededge1",
        "B06": "rededge2",
        "B07": "rededge3",
        "B08": "nir",
        "B8A": "nir08",
        "B09": "nir09",
        "B10": "cirrus",
        "B11": "swir16",
        "B12": "swir22",
        "QA60": "qa60",  # Stopped being generated 01-2022
    },
    "S2_L2SURF": {
        "B01": "coastal",
        "B02": "blue",
        "B03": "green",
        "B04": "red",
        "B05": "rededge1",
        "B06": "rededge2",
        "B07": "rededge3",
        "B08": "nir",
        "B8A": "nir08",
        "B09": "nir09",
        "B11": "swir16",
        "B12": "swir22",
    },
    "S2_L2RGB": {  # Collapse into single call to "visual"
        "TCI_R": "visual",
        "TCI_G": "visual",
        "TCI_B": "visual",
    },
    "LC08_L1TOA": {
        "B1": "coastal",
        "B2": "blue",
        "B3": "green",
        "B4": "red",
        "B5": "nir08",
        "B6": "swir16",
        "B7": "swir22",
        "B8": "B8",
        "B9": "cirrus",
        "B10": "lwir11",
        "B11": "lwir12",
        "QA_PIXEL": "qa_pixel",
        "QA_RADSAT": "qa_radsat",
        "SAA": "SAA",
        "SZA": "SZA",
        "VAA": "VAA",
        "VZA": "VZA",
    },
    "LC08_L2RGB": {
        "SR_B4": "red",
        "SR_B3": "green",
        "SR_B2": "blue",
    },
    "S1_L1C": {
        "VV": "vv",
        "VH": "vh",
        "HH": "hh",
        "HV": "hv",
    },
}

PC_COLLECTION_MAP = {
    "S2_L1TOA": "sentinel-2-l1c",
    "S2_L2RGB": "sentinel-2-l2a",
    "S2_L2SURF": "sentinel-2-l2a",
    "LC08_L1TOA": "landsat-8-l1",
    "LC08_L2RGB": "landsat-8-l2",
    "S1_L1C": "sentinel-1-grd",
}


ADLS_PREFIX = "01j9ajb2mdvmnkyhpahfevcy2t-sageport-main"


def _safe_split(value: str) -> List[str]:
    # Backwards-compatible wrapper (the implementation lives in asset_policy).
    return _safe_split_impl(value)


def _normalize_band_name(name: str) -> str:
    # Backwards-compatible wrapper (the implementation lives in asset_policy).
    return _normalize_band_name_impl(name)


class GeoanalyticsDownloader:
    def __init__(
        self,
        config_path: str,
        dry_run: bool = False,
        safe_mode: bool = False,
        max_items: int | None = None,
        max_assets_per_item: int | None = None,
        skip_uploads: bool = False,
        log_path: str | None = None,
        aoi_path_override: str | None = None,
        start_date_override: str | None = None,
        end_date_override: str | None = None,
    ):
        # Some user configs may repeat options (e.g., `stac_endpoints`) due to
        # copy/paste. The default strict parser raises DuplicateOptionError; we
        # prefer to accept the file and use the last-seen value.
        config = configparser.ConfigParser(strict=False)
        config.read(config_path)
        if "GLOBAL" not in config:
            raise ValueError("download.ini must contain a [GLOBAL] section")

        self.config = config
        self.global_config = config["GLOBAL"]
        self.dry_run = dry_run

        # Safety valves for browser-based VS Code sessions.
        # - safe_mode: best-effort conservative defaults (less memory/logging)
        # - max_items/max_assets_per_item: cap work so you can inspect output quickly
        # - skip_uploads: validate local outputs without pushing to remote stores
        self.safe_mode = safe_mode
        self.max_items = max_items
        self.max_assets_per_item = max_assets_per_item
        self.skip_uploads = skip_uploads

        self.log_path = log_path
        self._log_fh = None
        if self.log_path:
            Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(self.log_path, "a", encoding="utf-8")

        # QA artifacts (local quicklooks + metadata) to validate alignment.
        # These are local-only and safe for browser VS Code inspection.
        self.qa_quicklooks = True
        self.qa_quicklook_dir = str(Path("./qa").resolve())
        self.qa_quicklook_margin = 0.35

        # Central policy objects to reduce branching.
        self.asset_policy = AssetDownloadPolicy()

        io_config = IOConfig(
            adl_account=self.global_config.get("adl_account_name"),
        )
        self.io_client = GeoanalyticsIOClient(io_config)

        self.aoi_path = aoi_path_override or self.global_config.get("aoi") or ""
        if not self.aoi_path:
            raise ValueError(
                "AOI path must be defined either in GLOBAL section or via --aoi"
            )
        if not Path(self.aoi_path).exists() and not (
            self.aoi_path.startswith("abfs://") or self.aoi_path.startswith("az://")
        ):
            raise FileNotFoundError(f"AOI file not found: {self.aoi_path}")
        elif self.aoi_path.startswith("abfs://"):
            self.aoi_name = Path(self.aoi_path.split("/")[-1]).stem
            self.bbox = self.io_client.load_remote_aoi(self.aoi_path)
        else:
            self.aoi_path = str(Path(self.aoi_path).resolve())
            self.aoi_name = Path(self.aoi_path).stem
            self.bbox = self._load_aoi_bbox(self.aoi_path)

        self.clip_to_aoi = (
            self.global_config.get("clip_to_aoi", "false").lower() == "true"
        )

        self.start_date = pendulum.parse(
            start_date_override or self.global_config.get("start_date")
        )
        self.end_date = pendulum.parse(
            end_date_override or self.global_config.get("end_date")
        )
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")

        self.save_dir = self.global_config.get("save_dir", "")
        self.cloud_threshold = float(self.global_config.get("cloud_percentage", 100))

        # STAC search policy (depends on cloud_threshold).
        self.stac_searcher = StacSearcher(
            StacSearchConfig(
                endpoints=STAC_ENDPOINTS, cloud_threshold=self.cloud_threshold
            )
        )

        self.target = self.global_config.get("target", "all")
        self.asset_order = _safe_split(self.global_config.get("assets", ""))
        self.override_map = self._load_overrides()

        # Merge configuration
        self.merge_outputs = (
            self.global_config.get("merge_outputs", "false").lower() == "true"
        )
        self.temp_download_dir = self.global_config.get("temp_download_dir", "")

        # Optional: for SAR only, upload un-aligned per-band GeoTIFFs alongside the
        # existing aligned/merged outputs. Useful for QA in external GIS tools.
        self.upload_raw_sar = (
            self.global_config.get("upload_raw_sar", "false").lower() == "true"
        )

        # Optional: for SAR only, upload an additional "aligned raw" per-band GeoTIFF
        # that includes the same SAR orientation fix used for correct clipping.
        self.upload_aligned_raw_sar = (
            self.global_config.get("upload_aligned_raw_sar", "false").lower() == "true"
        )

        # In safe mode, default to NOT generating extra SAR outputs unless
        # explicitly enabled per section. These aligned/raw paths can be very
        # large and significantly increase memory pressure.
        if self.safe_mode:
            self.upload_raw_sar = False
            self.upload_aligned_raw_sar = False

    def close(self) -> None:
        if getattr(self, "_log_fh", None) is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass

    def _emit(self, msg: str) -> None:
        """Write status messages without overwhelming the browser terminal.

        - Always writes to a log file if provided.
        - Prints only a subset when safe_mode is enabled.
        """

        if getattr(self, "_log_fh", None) is not None:
            try:
                self._log_fh.write(msg.rstrip("\n") + "\n")
                self._log_fh.flush()
            except Exception:
                pass

        if self.safe_mode:
            # Keep terminal output tame.
            if msg.lstrip().startswith("Source href"):
                return
            if "writing" in msg.lower() and "memory file" in msg.lower():
                return
        print(msg)

        # Output format: "cog" (default) or "zarr"
        self.output_format = self.global_config.get("output_format", "cog").lower()
        if self.output_format not in ("cog", "zarr"):
            raise ValueError(
                f"output_format must be 'cog' or 'zarr', got '{self.output_format}'"
            )

        # Zarr-specific settings
        self.zarr_chunks = self._parse_chunks(
            self.global_config.get("zarr_chunks", "1,512,512")
        )

        # Hierarchical Zarr mode: single store with groups per scene
        # If True, creates structure: <collection>.zarr/<date>/bands/ and <date>/merged/
        self.hierarchical_zarr = (
            self.global_config.get("hierarchical_zarr", "false").lower() == "true"
        )

        # Whether to write individual bands to Zarr (only used in hierarchical mode)
        self.write_individual_bands = (
            self.global_config.get("write_individual_bands", "true").lower() == "true"
        )

        # Path to the root Zarr store (only used in hierarchical mode)
        # If not set, will be derived from save_dir and collection name
        self.zarr_store_path = self.global_config.get("zarr_store_path", "")

    def _parse_chunks(self, chunks_str: str) -> tuple[int, int, int]:
        """Parse chunk size string like '1,512,512' into a tuple."""
        try:
            parts = [int(x.strip()) for x in chunks_str.split(",")]
            if len(parts) == 3:
                return tuple(parts)
            elif len(parts) == 2:
                return (1, parts[0], parts[1])
            elif len(parts) == 1:
                return (1, parts[0], parts[0])
            else:
                raise ValueError("Too many values")
        except Exception:
            logger.warning(
                "Could not parse zarr_chunks '%s', using default (1, 512, 512)",
                chunks_str,
            )
            return (1, 512, 512)

    def run(self) -> None:
        logger.info("Starting Geoanalytics download workflow")
        try:
            for section in self.asset_order:
                if section not in self.config:
                    self._emit(f"Skipping {section}: configuration missing")
                    continue
                asset_config = self.config[section]
                collection = STAC_COLLECTION_MAP.get(section)
                if collection is None:
                    self._emit(f"No STAC mapping available for {section}; skipping")
                    continue

                include_bands = _safe_split(asset_config.get("include_bands", ""))
                try:
                    resolution = int(asset_config.get("resolution", "0"))
                except ValueError:
                    resolution = 0

                anonym = asset_config.get("anonym", section)
                asset_savedir = asset_config.get("save_dir", "misc")
                section_endpoints = self._resolve_stac_endpoints(section)

                # Check if this section should merge outputs
                section_merge = asset_config.get("merge_outputs", "").lower()
                should_merge = section_merge == "true" or (
                    section_merge == "" and self.merge_outputs
                )

                processed_items = 0
                for current_date in self._iter_dates():
                    date_str = current_date.format("YYYY-MM-DD")
                    if self.max_items is not None and processed_items >= int(
                        self.max_items
                    ):
                        self._emit(
                            f"Reached --max-items={self.max_items} for section {section}; stopping early"
                        )
                        break

                    self._emit(f"Processing {section} for {date_str}")
                    if self.dry_run:
                        self._emit(
                            f"  [dry-run] would search {collection} for {date_str}"
                        )
                        continue

                    item = self._find_stac_item(collection, current_date, section_endpoints)
                    if item is None:
                        self._emit(
                            f"  No STAC item found for {collection} on {date_str}"
                        )
                        continue

                    processed_items += 1

                    matched_assets = self._match_assets(section, item, include_bands)
                    if not matched_assets:
                        self._emit(
                            f"  No matching assets found for {section} on {date_str}"
                        )
                        continue

                    if should_merge:
                        # Download to temp directory and merge
                        self._download_and_merge_assets(
                            section=section,
                            item=item,
                            matched_assets=matched_assets,
                            asset_savedir=asset_savedir,
                            anonym=anonym,
                            current_date=current_date,
                            resolution=resolution,
                            clip_aoi=self.clip_to_aoi,
                        )
                    else:
                        # Original behavior: download each asset separately
                        self._download_assets_individually(
                            section=section,
                            item=item,
                            matched_assets=matched_assets,
                            asset_savedir=asset_savedir,
                            anonym=anonym,
                            current_date=current_date,
                            resolution=resolution,
                            clip_aoi=self.clip_to_aoi,
                        )
        finally:
            self.io_client.close()

    def _download_assets_individually(
        self,
        section: str,
        item,
        matched_assets: List[str],
        asset_savedir: str,
        anonym: str,
        current_date: pendulum.DateTime,
        resolution: int,
        clip_aoi: bool,
    ) -> None:
        """Download each asset as a separate file (original behavior)."""
        date_str = current_date.format("YYYY-MM-DD")
        for asset_key in matched_assets:
            asset = item.assets[asset_key]
            suffix = Path(asset.href).suffix or ".dat"
            proposal = asset_key.replace("/", "_")
            filename = (
                f"{section}_{date_str}_{proposal}_{self.aoi_name}_{resolution}m{suffix}"
            )
            target_path = self._build_target_path(
                asset_savedir,
                anonym,
                current_date.format("YYYYMMDD"),
                filename,
            )

            raster_bands = asset.extra_fields.get("raster:bands", [])
            raster_info = raster_bands[0] if raster_bands else {}
            dtype = raster_info.get("data_type")
            nodata = raster_info.get("nodata")

            print(f"  Downloading asset {asset_key} to {target_path}")
            try:
                self._copy_asset(
                    asset.href,
                    target_path,
                    dtype,
                    nodata,
                    clip_aoi,
                )
            except Exception as exc:
                print(f"    Failed to copy {asset.href}: {exc}")

    def _download_and_merge_assets(
        self,
        section: str,
        item,
        matched_assets: List[str],
        asset_savedir: str,
        anonym: str,
        current_date: pendulum.DateTime,
        resolution: int,
        clip_aoi: bool = False,
    ) -> None:
        """Download assets to temp directory and merge into a single file (COG or Zarr)."""
        import shutil

        date_str = current_date.format("YYYY-MM-DD")
        target_resolution = (
            float(resolution) if resolution is not None and resolution > 0 else None
        )
        target_crs = self._resolve_target_crs(section)

        # Check for section-level output format override
        section_format = ""
        if section in self.config:
            section_format = self.config[section].get("output_format", "").lower()
        output_format = (
            section_format if section_format in ("cog", "zarr") else self.output_format
        )

        # Check for hierarchical Zarr mode
        section_hierarchical = self.config[section].get("hierarchical_zarr", "").lower()
        use_hierarchical = section_hierarchical == "true" or (
            section_hierarchical == "" and self.hierarchical_zarr
        )

        # Create temp directory for downloads
        if self.temp_download_dir:
            temp_base = Path(self.temp_download_dir)
            temp_base.mkdir(parents=True, exist_ok=True)
            temp_dir = temp_base / f"{section}_{date_str}_{self.aoi_name}"
            temp_dir.mkdir(exist_ok=True)
            temp_dir_path = str(temp_dir)
        else:
            import tempfile

            temp_dir_path = tempfile.mkdtemp(prefix=f"{section}_{date_str}_")

        downloaded_files: List[str] = []
        bandnames: List[str] = []

        # Get item metadata for attributes (needed early for hierarchical mode)
        item_id = item.id if hasattr(item, "id") else "unknown"
        cloud_pct = (
            item.properties.get("eo:cloud_cover", None)
            if hasattr(item, "properties")
            else None
        )

        # For hierarchical mode, set up the store path early
        store_path = None
        scene_id = None
        if use_hierarchical and output_format == "zarr":
            scene_id = current_date.format("YYYYMMDD")
            if self.zarr_store_path:
                store_path = self.zarr_store_path
            else:
                store_filename = f"{section}_{self.aoi_name}_{resolution}m.zarr"
                store_path = self._build_target_path(
                    asset_savedir, anonym, "", store_filename
                ).rstrip("/")

            # Ensure the store exists
            self._emit(f"  Opening/creating Zarr store: {store_path}")
            open_or_create_zarr_store(store_path, self.io_client, mode="a")

        # For SAR data, fetch a Sentinel-2 reference for spatial alignment
        # Sentinel-1 GRD on AWS Earth Search lacks internal georeferencing
        reference_data = None
        collection = STAC_COLLECTION_MAP.get(section)
        is_sar_section = bool(collection and self._is_sar_collection(collection))

        upload_raw_sar = self._resolve_upload_raw_sar(section) if is_sar_section else False
        upload_aligned_raw_sar = (
            self._resolve_upload_aligned_raw_sar(section) if is_sar_section else False
        )

        if is_sar_section:
            reference_data = self._fetch_reference_data(
                bbox=self.bbox,
                target_crs=target_crs,
                target_resolution=target_resolution,
            )

        # Extract orbit state for SAR data orientation
        orbit_state = None
        if hasattr(item, "properties"):
            orbit_state = item.properties.get("sat:orbit_state")
            if orbit_state:
                self._emit(f"  SAR orbit state: {orbit_state}")

        try:
            # Apply developer safety valve caps.
            asset_keys = list(matched_assets)
            if self.max_assets_per_item is not None:
                asset_keys = asset_keys[: max(0, int(self.max_assets_per_item))]

            self._emit(f"  Downloading {len(asset_keys)} assets...")

            for asset_key in asset_keys:
                if not self.asset_policy.is_supported_asset(section, asset_key):
                    self._emit(
                        f"    Skipping asset {asset_key} (unsupported by policy)"
                    )
                    continue
                asset = item.assets[asset_key]
                suffix = Path(asset.href).suffix or ".tif"
                # Ensure we're working with tif for merging
                if suffix.lower() in (".jp2", ".jpx", ".jpeg2000"):
                    suffix = ".tif"

                local_filename = f"{asset_key.replace('/', '_')}{suffix}"
                local_path = os.path.join(temp_dir_path, local_filename)

                raw_local_path = None
                if upload_raw_sar:
                    raw_local_path = os.path.join(
                        temp_dir_path, f"raw_{asset_key.replace('/', '_')}{suffix}"
                    )

                aligned_raw_local_path = None
                if upload_aligned_raw_sar:
                    aligned_raw_local_path = os.path.join(
                        temp_dir_path,
                        f"aligned_raw_{asset_key.replace('/', '_')}{suffix}",
                    )

                raster_bands = asset.extra_fields.get("raster:bands", [])
                raster_info = raster_bands[0] if raster_bands else {}
                dtype = raster_info.get("data_type")
                nodata = raster_info.get("nodata")

                self._emit(f"    Downloading {asset_key}...")
                self._emit(f"      Source href: {asset.href}")
                try:
                    spatial_metadata = self._extract_proj_metadata(asset, item)

                    # Optionally save a raw, un-aligned GeoTIFF (SAR only) alongside
                    # the existing aligned/merged workflow.
                    if raw_local_path is not None:
                        self._download_to_local(
                            asset.href,
                            raw_local_path,
                            dtype,
                            nodata,
                            spatial_metadata=spatial_metadata,
                            asset_name=f"raw:{asset_key}",
                            fallback_crs=target_crs,
                            reference_data=None,
                            orbit_state=orbit_state,
                            align_to_reference=False,
                            clip_bbox=self.bbox if clip_aoi else None,
                        )

                        raw_filename = (
                            f"{section}_{date_str}_{asset_key.replace('/', '_')}_RAW.tif"
                        )
                        raw_target_path = self._build_target_path(
                            asset_savedir,
                            anonym,
                            current_date.format("YYYYMMDD"),
                            raw_filename,
                        )

                        if os.path.exists(raw_local_path) and os.path.getsize(raw_local_path) > 1024:
                            if not self.skip_uploads:
                                self.io_client.submit_copy(
                                    raw_local_path,
                                    raw_target_path,
                                    dtype="uint16" if dtype is None else dtype,
                                    nodata=nodata,
                                    clip_bbox=None,
                                )
                        else:
                            self._emit(
                                f"      Warning: Raw SAR output missing/too small for {asset_key}; not uploading"
                            )

                    if aligned_raw_local_path is not None:
                        self._download_to_local(
                            asset.href,
                            aligned_raw_local_path,
                            dtype,
                            nodata,
                            spatial_metadata=spatial_metadata,
                            asset_name=f"aligned_raw:{asset_key}",
                            fallback_crs=target_crs,
                            # Special marker understood by RasterDownloader/SarAligner
                            # to apply orbit-based orientation correction but skip
                            # S2 reference reproject.
                            reference_data="__ORBIT_ORIENT__",
                            orbit_state=orbit_state,
                            align_to_reference=False,
                            clip_bbox=self.bbox if clip_aoi else None,
                        )

                        aligned_raw_filename = (
                            f"{section}_{date_str}_{asset_key.replace('/', '_')}_ALIGNED_RAW.tif"
                        )
                        aligned_raw_target_path = self._build_target_path(
                            asset_savedir,
                            anonym,
                            current_date.format("YYYYMMDD"),
                            aligned_raw_filename,
                        )

                        if (
                            os.path.exists(aligned_raw_local_path)
                            and os.path.getsize(aligned_raw_local_path) > 1024
                        ):
                            if not self.skip_uploads:
                                self.io_client.submit_copy(
                                    aligned_raw_local_path,
                                    aligned_raw_target_path,
                                    dtype="uint16" if dtype is None else dtype,
                                    nodata=nodata,
                                    clip_bbox=None,
                                )
                        else:
                            self._emit(
                                f"      Warning: Aligned-raw SAR output missing/too small for {asset_key}; not uploading"
                            )

                    # Download to local temp file (convert to GeoTIFF in process)
                    self._download_to_local(
                        asset.href,
                        local_path,
                        dtype,
                        nodata,
                        spatial_metadata=spatial_metadata,
                        asset_name=asset_key,
                        fallback_crs=target_crs,
                        reference_data=reference_data,
                        orbit_state=orbit_state,
                        align_to_reference=bool(is_sar_section and reference_data is not None),
                        clip_bbox=self.bbox if clip_aoi else None,
                    )
                    if (
                        os.path.exists(local_path)
                        and os.path.getsize(local_path) > 1024
                    ):
                        downloaded_files.append(local_path)
                        bandnames.append(asset_key)

                        # Hierarchical mode: write band to Zarr immediately after download
                        if (
                            use_hierarchical
                            and output_format == "zarr"
                            and self.write_individual_bands
                        ):
                            self._emit(f"      Writing {asset_key} to Zarr...")
                            try:
                                write_band_to_zarr_group(
                                    tif_path=local_path,
                                    store_path=store_path,
                                    group_path=f"{scene_id}/bands",
                                    band_name=asset_key,
                                    io_client=self.io_client,
                                    chunks=(self.zarr_chunks[1], self.zarr_chunks[2]),
                                    stac_item_id=item_id,
                                    date=date_str,
                                    cloud_cover=cloud_pct,
                                )
                            except Exception as band_exc:
                                self._emit(
                                    f"      Warning: Failed to write band to Zarr: {band_exc}"
                                )

                except Exception as exc:
                    self._emit(f"      Failed to download {asset.href}: {exc}")

            if not downloaded_files:
                self._emit(
                    f"  No assets successfully downloaded for {section} on {date_str}"
                )
                return

            # Hierarchical Zarr mode: now create the merged group from downloaded files
            if use_hierarchical and output_format == "zarr":
                self._emit(
                    f"  Creating merged group from {len(downloaded_files)} bands..."
                )
                # Determine clip_bbox for this operation
                clip_bbox = self.bbox if clip_aoi else None
                try:
                    context = MergeContext(
                        section=section,
                        date_str=date_str,
                        item_id=item_id,
                        aoi_name=self.aoi_name,
                        resolution=resolution,
                        clip_bbox=clip_bbox,
                        cloud_cover=cloud_pct,
                        target_crs=target_crs,
                        target_resolution=target_resolution,
                    )

                    merger = build_merge_strategy(
                        output_format="zarr",
                        hierarchical=True,
                        hierarchical_spec=HierarchicalZarrSpec(
                            store_path=str(store_path),
                            group_path=f"{scene_id}/merged",
                        ),
                    )
                    merger.merge(
                        tif_files=downloaded_files,
                        bandnames=bandnames,
                        output_path=str(store_path),
                        io_client=self.io_client,
                        context=context,
                        chunks=self.zarr_chunks,
                    )
                    self._emit(
                        f"  Successfully ingested scene to {store_path}/{scene_id}/"
                    )
                except Exception as exc:
                    self._emit(f"  Failed to create merged group: {exc}")
                    raise
            else:
                # Original mode: separate file per scene
                self._merge_to_single_file(
                    section=section,
                    downloaded_files=downloaded_files,
                    bandnames=bandnames,
                    asset_savedir=asset_savedir,
                    anonym=anonym,
                    current_date=current_date,
                    resolution=resolution,
                    output_format=output_format,
                    item_id=item_id,
                    cloud_pct=cloud_pct,
                    clip_aoi=clip_aoi,
                    target_crs=target_crs,
                )

        finally:
            # Clean up temp directory
            if os.path.exists(temp_dir_path):
                shutil.rmtree(temp_dir_path, ignore_errors=True)

    def _merge_to_single_file(
        self,
        section: str,
        downloaded_files: List[str],
        bandnames: List[str],
        asset_savedir: str,
        anonym: str,
        current_date: pendulum.DateTime,
        resolution: int,
        output_format: str,
        item_id: str,
        cloud_pct: float | None,
        clip_aoi: bool = False,
        target_crs: Optional[str] = None,
    ) -> None:
        """Merge downloaded files into a single output file (original behavior)."""
        date_str = current_date.format("YYYY-MM-DD")
        target_resolution = (
            float(resolution) if resolution is not None and resolution > 0 else None
        )
        target_crs = target_crs or self._resolve_target_crs(section)

        # Determine clip_bbox from clip_aoi flag
        clip_bbox = self.bbox if clip_aoi else None

        # Build output path for merged file
        if output_format == "zarr":
            merged_filename = (
                f"{section}_{date_str}_{self.aoi_name}_{resolution}m_merged.zarr"
            )
        else:
            merged_filename = (
                f"{section}_{date_str}_{self.aoi_name}_{resolution}m_merged.tif"
            )

        merged_target_path = self._build_target_path(
            asset_savedir,
            anonym,
            current_date.format("YYYYMMDD"),
            merged_filename,
        )

        self._emit(
            f"  Merging {len(downloaded_files)} files into {merged_target_path} (format: {output_format})..."
        )

        try:
            context = MergeContext(
                section=section,
                date_str=date_str,
                item_id=item_id,
                aoi_name=self.aoi_name,
                resolution=resolution,
                clip_bbox=clip_bbox,
                cloud_cover=cloud_pct,
                target_crs=target_crs,
                target_resolution=target_resolution,
            )

            merger = build_merge_strategy(
                output_format=output_format, hierarchical=False
            )
            merger.merge(
                tif_files=downloaded_files,
                bandnames=bandnames,
                output_path=merged_target_path,
                io_client=self.io_client,
                context=context,
                chunks=self.zarr_chunks,
            )
            self._emit(f"  Successfully merged and uploaded to {merged_target_path}")

            # QA artifacts: only possible when the merged output is local.
            # In most runs we upload directly to cloud storage; however, the merge
            # strategy often creates a local temp file first. If you want QA for
            # remote-only outputs, use io_client to stage a local copy.
            try:
                if self.qa_quicklooks and isinstance(merged_target_path, str) and merged_target_path.endswith(".tif"):
                    # If the output is remote (abfs/s3), we can't open it without
                    # extra plumbing. Skip and rely on per-band quicklooks.
                    if merged_target_path.startswith("abfs://") or merged_target_path.startswith("s3://"):
                        self._emit("  QA quicklook skipped for remote merged output (enable staging if needed)")
                    else:
                        qa_dir = Path(self.qa_quicklook_dir)
                        qa_dir.mkdir(parents=True, exist_ok=True)
                        safe_date = current_date.format("YYYYMMDD")
                        base = f"{section}_{safe_date}_{self.aoi_name}_{resolution}m_merged"
                        meta_path = str(qa_dir / f"{base}.json")
                        png_path = str(qa_dir / f"{base}.png")

                        write_raster_metadata_json(raster_path=merged_target_path, output_json=meta_path)
                        render_quicklook(
                            QuicklookRequest(
                                raster_path=merged_target_path,
                                output_png=png_path,
                                title=f"{section} {date_str} merged ({self.aoi_name})",
                                aoi_bbox=self.bbox,
                                margin_fraction=self.qa_quicklook_margin,
                            )
                        )
                        self._emit(f"  Wrote QA quicklook: {png_path}")
            except Exception as qa_exc:
                self._emit(f"  Warning: QA quicklook generation failed: {qa_exc}")
        except Exception as exc:
            self._emit(f"  Failed to merge assets: {exc}")
            raise

    def _download_to_local(
        self,
        href: str,
        local_path: str,
        dtype: str,
        nodata: float | None,
        spatial_metadata: Optional[Dict[str, Any]] = None,
        asset_name: Optional[str] = None,
        fallback_crs: Optional[str] = None,
        reference_data=None,
        orbit_state: Optional[str] = None,
        align_to_reference: bool = False,
        clip_bbox: Optional[List[float]] = None,
    ) -> None:
        """Download a remote asset to a local file.

        This is a thin wrapper for backwards compatibility. The implementation
        lives in `raster_io.RasterDownloader`.
        """

        req = DownloadToLocalRequest(
            href=href,
            local_path=local_path,
            dtype=dtype,
            nodata=nodata,
            spatial_metadata=spatial_metadata,
            asset_name=asset_name,
            fallback_crs=fallback_crs,
            reference_data=reference_data,
            orbit_state=orbit_state,
            align_to_reference=align_to_reference,
            clip_bbox=clip_bbox,
            clip_bbox_crs="EPSG:4326",
        )

        RasterDownloader(self.io_client).download_to_local(req)

    def _is_sar_collection(self, collection: str) -> bool:
        # Backwards-compatible shim kept because orchestration code still calls this.
        return self.stac_searcher.is_sar_collection(collection)

    def _compute_aoi_coverage(self, item, aoi_bbox: List[float]) -> float:
        """Compute how much of the AOI is covered by the item's geometry.

        Returns a value between 0 and 1 representing the fraction of AOI covered.
        """
        try:
            from shapely.geometry import box, shape

            # Create AOI polygon from bbox
            aoi_polygon = box(aoi_bbox[0], aoi_bbox[1], aoi_bbox[2], aoi_bbox[3])

            # Get item geometry
            item_geom = getattr(item, "geometry", None)
            if item_geom is None:
                # Fallback to bbox if no geometry
                item_bbox = getattr(item, "bbox", None)
                if item_bbox:
                    item_polygon = box(*item_bbox)
                else:
                    return 0.0
            else:
                item_polygon = shape(item_geom)

            # Calculate intersection
            intersection = aoi_polygon.intersection(item_polygon)
            if intersection.is_empty:
                return 0.0

            coverage = intersection.area / aoi_polygon.area
            return min(coverage, 1.0)
        except Exception as exc:
            print(f"    Warning: Could not compute coverage: {exc}")
            return 0.0

    def _find_stac_item(
        self, collection: str, date: pendulum.DateTime, endpoints: List[str]
    ):
        period = f"{date.format('YYYY-MM-DD')}/{date.add(days=1).format('YYYY-MM-DD')}"
        return self.stac_searcher.find_item(
            collection=collection,
            bbox=self.bbox,
            period=period,
            endpoints=endpoints or STAC_ENDPOINTS,
        )

    def _find_stac_item_sar(self, collection: str, period: str, endpoints: List[str]):
        """Find STAC item for SAR collections with proper spatial coverage validation.

        SAR data (like Sentinel-1) comes in narrow swaths, so multiple scenes may
        have overlapping bounding boxes but different actual coverage of the AOI.
        We fetch multiple candidates and select the one with best AOI coverage.
        """
        min_coverage_threshold = 0.5  # Require at least 50% AOI coverage

        for endpoint in endpoints:
            client = self._open_stac_client(endpoint)
            if client is None:
                continue

            try:
                # Fetch multiple items to find the best coverage
                search = client.search(
                    collections=[collection],
                    bbox=self.bbox,
                    datetime=period,
                    limit=10,  # Get multiple candidates
                )
                items = list(search.items())

                if not items:
                    self._emit(f"  No SAR items found at {endpoint} for {period}")
                    continue

                consider_items = items
                if self.max_items is not None:
                    consider_items = consider_items[: max(0, int(self.max_items))]

                self._emit(
                    f"  Found {len(items)} SAR candidate scenes, evaluating coverage ({len(consider_items)} considered)..."
                )

                # Evaluate each item's coverage of the AOI
                best_item = None
                best_coverage = 0.0

                for item in consider_items:
                    coverage = self._compute_aoi_coverage(item, self.bbox)
                    item_id = getattr(item, "id", "unknown")
                    item_bbox = getattr(item, "bbox", None)

                    # Detailed per-item prints can be extremely verbose.
                    if not self.safe_mode:
                        self._emit(f"    Item {item_id}:")
                        self._emit(f"      Item bbox: {item_bbox}")
                        self._emit(f"      AOI bbox: {self.bbox}")
                        self._emit(f"      AOI coverage: {coverage * 100:.1f}%")

                    if coverage > best_coverage:
                        best_coverage = coverage
                        best_item = item

                if best_item is not None:
                    if best_coverage < min_coverage_threshold:
                        self._emit(
                            f"  Warning: Best SAR item only covers {best_coverage * 100:.1f}% of AOI "
                            f"(threshold: {min_coverage_threshold * 100:.0f}%)"
                        )
                    best_id = getattr(best_item, "id", "unknown")
                    best_bbox = getattr(best_item, "bbox", None)
                    self._emit(
                        f"  Selected SAR item: {best_id} with {best_coverage * 100:.1f}% coverage"
                    )
                    self._emit(f"  Selected item bbox: {best_bbox}")
                    return best_item

            except Exception as exc:
                self._emit(f"  SAR STAC search failed at {endpoint}: {exc}")

        return None

    def _iter_dates(self) -> Iterable[pendulum.DateTime]:
        current = self.start_date
        while current <= self.end_date:
            yield current
            current = current.add(days=1)

    def _build_asset_alias_map(self, section: str, item) -> dict[str, List[str]]:
        """Construct a lookup that maps normalized band tokens to actual asset keys."""

        overrides = (
            self.override_map.get(section.upper())
            if hasattr(self, "override_map")
            else None
        )
        return build_asset_alias_map(
            section=section,
            item_assets=item.assets,
            earth_search_asset_map=EARTH_SEARCH_ASSET_MAP,
            overrides=overrides,
        )

    def _load_overrides(self) -> dict[str, dict[str, List[str]]]:
        overrides: dict[str, dict[str, List[str]]] = {}
        if "OVERRIDE" not in self.config:
            return overrides

        override_section = self.config["OVERRIDE"]
        for raw_key, raw_value in override_section.items():
            if "_" not in raw_key:
                print(
                    f"  Override entry '{raw_key}' is missing an underscore; expected format DATASET_BAND. Skipping."
                )
                continue

            dataset_key, band_key = raw_key.rsplit("_", 1)
            dataset_key = dataset_key.strip().upper()
            normalized_band = _normalize_band_name(band_key)

            if not dataset_key or not normalized_band:
                print(
                    f"  Override entry '{raw_key}' could not be parsed into dataset and band tokens. Skipping."
                )
                continue

            asset_names = _safe_split(raw_value)
            if not asset_names:
                print(
                    f"  Override entry '{raw_key}' does not specify any asset names. Skipping."
                )
                continue

            overrides.setdefault(dataset_key, {})[normalized_band] = asset_names

        return overrides

    def _resolve_target_crs(self, section: str) -> Optional[str]:
        raw_value = self._get_section_option(section, "target_crs")
        if not raw_value:
            raw_value = self.global_config.get("target_crs", "")

        return self._normalize_crs_value(raw_value)

    def _get_section_option(self, section: str, option: str) -> str:
        if section in self.config and option in self.config[section]:
            return self.config[section].get(option, "").strip()
        return ""

    def _normalize_crs_value(self, raw_value: str | None) -> Optional[str]:
        if not raw_value:
            return None

        value = raw_value.strip()
        if not value:
            return None

        upper_value = value.upper()
        if upper_value in {"AUTO", "AUTO_UTM", "AUTO-UTM"}:
            return self._infer_utm_epsg()

        if upper_value.startswith("EPSG:"):
            return upper_value

        if value.isdigit():
            return f"EPSG:{int(value)}"

        return value

    def _infer_utm_epsg(self) -> Optional[str]:
        if not getattr(self, "bbox", None):
            return None

        minx, miny, maxx, maxy = self.bbox
        center_lon = (minx + maxx) / 2.0
        center_lat = (miny + maxy) / 2.0

        zone = int((center_lon + 180) // 6) + 1
        zone = max(1, min(zone, 60))
        if center_lat >= 0:
            return f"EPSG:{32600 + zone}"
        return f"EPSG:{32700 + zone}"

    def _resolve_stac_endpoints(self, section: str) -> List[str]:
        section_value = self._get_section_option(section, "stac_endpoints")
        if section_value:
            return _safe_split(section_value)

        global_value = self.global_config.get("stac_endpoints", "")
        if global_value:
            parsed = _safe_split(global_value)
            if parsed:
                return parsed

        return STAC_ENDPOINTS

    def _resolve_upload_raw_sar(self, section: str) -> bool:
        """Resolve upload_raw_sar flag with per-section override.

        GLOBAL.upload_raw_sar can be overridden by [<SECTION>].upload_raw_sar.
        """
        section_value = self._get_section_option(section, "upload_raw_sar")
        if section_value:
            return section_value.lower() == "true"
        return bool(getattr(self, "upload_raw_sar", False))

    def _resolve_upload_aligned_raw_sar(self, section: str) -> bool:
        """Resolve upload_aligned_raw_sar flag with per-section override.

        GLOBAL.upload_aligned_raw_sar can be overridden by
        [<SECTION>].upload_aligned_raw_sar.
        """

        section_value = self._get_section_option(section, "upload_aligned_raw_sar")
        if section_value:
            return section_value.lower() == "true"
        return bool(getattr(self, "upload_aligned_raw_sar", False))

    def _fetch_reference_data(
        self,
        *,
        bbox: List[float],
        target_crs: Optional[str],
        target_resolution: Optional[float],
    ):
        """Fetch a reference raster for SAR alignment.

        Sentinel-1 GRD assets from AWS Earth Search can lack usable internal
        georeferencing. The historical downloader aligned SAR rasters against a
        Sentinel-2 reference grid.

        This method returns a small rioxarray DataArray (usually a single band)
        that can be used as the reference for `rio.reproject_match(...)`.

        If no reference is found or dependencies are missing, returns None.
        """

        # Choose a stable optical collection + band for referencing.
        collection = "sentinel-2-l2a"
        band_asset_candidates = [
            # Prefer common Earth Search asset keys first.
            "B02",
            "blue",
            "B03",
            "green",
            "B04",
            "red",
        ]

        # Use a short period around the start date so the reference is likely
        # to exist. This is good enough for defining a grid in the AOI.
        start = getattr(self, "start_date", None)
        if start is None:
            return None
        period = f"{start.format('YYYY-MM-DD')}/{start.add(days=1).format('YYYY-MM-DD')}"

        item = self.stac_searcher.find_item(
            collection=collection,
            bbox=bbox,
            period=period,
            endpoints=self._resolve_stac_endpoints("S2_L2SURF"),
        )
        if item is None:
            logger.warning("No Sentinel-2 reference item found for period %s", period)
            return None

        asset_key = None
        for candidate in band_asset_candidates:
            if hasattr(item, "assets") and candidate in item.assets:
                asset_key = candidate
                break
        if asset_key is None:
            logger.warning("Reference item %s has no expected band assets", getattr(item, "id", "unknown"))
            return None

        href = item.assets[asset_key].href

        # Download the reference asset to a temp file and open it with rioxarray.
        # We keep it local because downstream alignment happens locally.
        try:
            import tempfile
            import fsspec
            import rioxarray as rxr

            reader_opts = self.io_client._storage_options(href)
            with fsspec.open(href, "rb", **reader_opts) as reader_file:
                with rxr.open_rasterio(reader_file) as ds:
                    # Ensure CRS/transform exist as best-effort.
                    spatial_metadata = self._extract_proj_metadata(item.assets[asset_key], item)
                    ds = self._apply_spatial_metadata(
                        ds,
                        spatial_metadata,
                        asset_label=f"reference:{asset_key}",
                        fallback_crs=target_crs,
                    )

                    # If a target CRS/resolution is requested, snap reference to it.
                    if target_crs is not None:
                        ds = ds.rio.reproject(target_crs, resolution=target_resolution)
                    elif target_resolution is not None and ds.rio.crs is not None:
                        ds = ds.rio.reproject(ds.rio.crs, resolution=target_resolution)

                    # Return a DataArray suitable for `reproject_match`.
                    return ds
        except Exception as exc:
            logger.warning("Failed to fetch reference raster: %s", exc)
            return None

    def _open_stac_client(self, endpoint: str) -> Optional[Client]:
        # Backwards-compatible wrapper around the search policy.
        return self.stac_searcher.open_client(endpoint)

    def _extract_proj_metadata(self, asset, item) -> Dict[str, Any]:
        """Gather projection metadata from a STAC asset and its parent item."""
        metadata: Dict[str, Any] = {}
        keys = (
            "proj:epsg",
            "proj:wkt2",
            "proj:projjson",
            "proj:code",
            "proj:bbox",
            "proj:shape",
            "proj:transform",
        )

        sources: List[Dict[str, Any]] = []
        item_props = getattr(item, "properties", None)
        if isinstance(item_props, dict):
            sources.append(item_props)
        asset_fields = getattr(asset, "extra_fields", None)
        if isinstance(asset_fields, dict):
            sources.append(asset_fields)

        for source in sources:
            for key in keys:
                value = source.get(key)
                if value is not None:
                    metadata[key] = value

        return metadata

    @staticmethod
    def _crs_from_metadata(metadata: Dict[str, Any]) -> Optional[str]:
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
    def _transform_from_metadata(
        metadata: Dict[str, Any],
    ) -> Optional[Tuple[float, ...]]:
        if not metadata:
            return None

        transform = metadata.get("proj:transform")
        if transform and isinstance(transform, (list, tuple)):
            if len(transform) >= 6:
                # STAC proj:transform is row-major, same as Affine:
                # [a, b, c, d, e, f] = [scale_x, shear_x, translate_x, shear_y, scale_y, translate_y]
                return tuple(float(x) for x in transform[:6])

        bbox = metadata.get("proj:bbox")
        shape = metadata.get("proj:shape")
        if (
            isinstance(bbox, (list, tuple))
            and len(bbox) == 4
            and isinstance(shape, (list, tuple))
            and len(shape) == 2
        ):
            minx, miny, maxx, maxy = [float(val) for val in bbox]
            height, width = shape
            if width and height:
                # Affine(a, b, c, d, e, f):
                # a = pixel width (x scale)
                # b = row rotation (usually 0)
                # c = top-left x coordinate
                # d = column rotation (usually 0)
                # e = pixel height (y scale, usually negative)
                # f = top-left y coordinate
                xres = (maxx - minx) / float(width)
                yres = (miny - maxy) / float(height)  # negative for north-up
                return (xres, 0.0, minx, 0.0, yres, maxy)

        return None

    @staticmethod
    def _compute_transform_from_bbox(
        metadata: Dict[str, Any], actual_width: int, actual_height: int
    ) -> Optional[Tuple[float, ...]]:
        """Compute transform from proj:bbox using actual dataset dimensions.

        This is more reliable than using proj:transform directly, because:
        1. proj:shape in STAC may have height/width swapped vs actual file
        2. proj:transform may be calculated with wrong dimensions

        Args:
            metadata: STAC spatial metadata containing proj:bbox
            actual_width: Actual width of the dataset in pixels
            actual_height: Actual height of the dataset in pixels

        Returns:
            Affine transform coefficients (a, b, c, d, e, f) or None
        """
        bbox = metadata.get("proj:bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            return None

        if actual_width <= 0 or actual_height <= 0:
            return None

        minx, miny, maxx, maxy = [float(val) for val in bbox]

        # Calculate pixel sizes from bbox and actual dimensions
        xres = (maxx - minx) / float(actual_width)
        yres = (miny - maxy) / float(actual_height)  # negative for north-up

        # Affine: (scale_x, shear_x, translate_x, shear_y, scale_y, translate_y)
        return (xres, 0.0, minx, 0.0, yres, maxy)

    @staticmethod
    def _is_identity_or_invalid_transform(transform) -> bool:
        """Check if transform is missing, identity, or effectively invalid."""
        if transform is None:
            return True
        from rasterio.transform import Affine

        if not isinstance(transform, Affine):
            return True
        identity = Affine.identity()
        flipped = Affine(1.0, 0.0, 0.0, 0.0, -1.0, 0.0)
        return transform == identity or transform == flipped

    def _apply_spatial_metadata(
        self,
        dataset,
        spatial_metadata: Optional[Dict[str, Any]],
        asset_label: str,
        fallback_crs: Optional[str] = None,
    ):
        """Ensure an rioxarray dataset has CRS and transform metadata."""
        current_transform = dataset.rio.transform()
        print(f"      Source transform: {current_transform}")
        needs_transform = self._is_identity_or_invalid_transform(current_transform)
        print(f"      needs_transform (is identity/invalid): {needs_transform}")

        # Get actual dataset dimensions
        actual_width = dataset.rio.width
        actual_height = dataset.rio.height

        if spatial_metadata:
            crs_value = self._crs_from_metadata(spatial_metadata)
            print(
                f"      Dataset CRS before: {dataset.rio.crs}, STAC crs_value: {crs_value}, fallback_crs: {fallback_crs}"
            )
            if dataset.rio.crs is None and crs_value:
                print(f"      Applying CRS from STAC metadata: {crs_value}")
                dataset = dataset.rio.write_crs(crs_value, inplace=True)

            print(
                f"      STAC proj:transform raw: {spatial_metadata.get('proj:transform')}"
            )
            print(
                f"      STAC proj:bbox: {spatial_metadata.get('proj:bbox')}, proj:shape: {spatial_metadata.get('proj:shape')}"
            )
            print(
                f"      Actual dataset dimensions: width={actual_width}, height={actual_height}"
            )

            if needs_transform:
                # Calculate transform from proj:bbox using ACTUAL dataset dimensions
                # This handles cases where proj:shape doesn't match actual file dimensions
                # or where proj:transform was calculated with swapped dimensions
                transform_value = self._compute_transform_from_bbox(
                    spatial_metadata, actual_width, actual_height
                )

                if transform_value:
                    from rasterio.transform import Affine

                    new_transform = Affine(*transform_value)
                    print(f"      Computed transform from bbox: {new_transform}")
                    dataset = dataset.rio.write_transform(new_transform, inplace=True)
                    needs_transform = False

        print(
            f"      Dataset CRS after STAC: {dataset.rio.crs}, fallback_crs: {fallback_crs}"
        )
        if dataset.rio.crs is None and fallback_crs:
            print(f"      Applying fallback CRS: {fallback_crs}")
            dataset = dataset.rio.write_crs(fallback_crs, inplace=True)

        if dataset.rio.crs is None:
            print(
                f"      Warning: {asset_label} is missing CRS metadata and may fail downstream merges"
            )

        if needs_transform:
            print(
                f"      Warning: {asset_label} is missing transform metadata; output may not align correctly"
            )

        return dataset

    def _apply_overrides(
        self, section: str, alias_map: dict[str, List[str]], item
    ) -> None:
        overrides = (
            self.override_map.get(section.upper())
            if hasattr(self, "override_map")
            else None
        )
        if not overrides:
            return

        for band_name, preferred_assets in overrides.items():
            resolved: List[str] = []
            for candidate in preferred_assets:
                asset_key = candidate.strip()
                if not asset_key:
                    continue

                if asset_key in item.assets:
                    if asset_key not in resolved:
                        resolved.append(asset_key)
                    continue

                normalized_candidate = _normalize_band_name(asset_key)
                for fallback in alias_map.get(normalized_candidate, []):
                    if fallback not in resolved:
                        resolved.append(fallback)

            if not resolved:
                print(
                    f"  Override for {section} {band_name} did not match any available assets; leaving defaults in place."
                )
                continue

            alias_map[band_name] = resolved

    def _match_assets(self, section: str, item, include_bands: List[str]) -> List[str]:
        """Resolve configured band names to available STAC asset keys for a dataset."""
        if not include_bands:
            return self.asset_policy.filter_assets(section, list(item.assets.keys()))

        alias_map = self._build_asset_alias_map(section, item)
        matches: List[str] = []
        seen: set[str] = set()

        for band in include_bands:
            normalized = _normalize_band_name(band)
            if "visual" in normalized and "RGB" not in section.upper():
                continue
            for asset_name in alias_map.get(normalized, []):
                if not self.asset_policy.is_supported_asset(section, asset_name):
                    continue
                if "RGB" not in section.upper() and (
                    "visual" in asset_name or "visual" in normalized
                ):
                    continue
                if asset_name not in seen:
                    matches.append(asset_name)
                    seen.add(asset_name)

        if matches:
            return matches

        return self.asset_policy.filter_assets(section, list(item.assets.keys()))

    def _load_aoi_bbox(self, path: str) -> List[float]:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        features = []
        if data.get("type") == "FeatureCollection":
            features = data.get("features", [])
        elif data.get("type") == "Feature":
            features = [data]
        else:
            raise ValueError("AOI GeoJSON must contain a Feature or FeatureCollection")

        x_values: list[float] = []
        y_values: list[float] = []
        for feature in features:
            geometry = feature.get("geometry")
            if not geometry:
                continue
            self._accumulate_coords(geometry, x_values, y_values)

        if not x_values or not y_values:
            raise ValueError("AOI geometry did not contain coordinates")

        return [min(x_values), min(y_values), max(x_values), max(y_values)]

    def _accumulate_coords(
        self, geometry: dict, x_values: List[float], y_values: List[float]
    ) -> None:
        geom_type = geometry.get("type")
        coords = geometry.get("coordinates")
        if coords is None:
            return

        if geom_type == "Polygon":
            for ring in coords:
                for coord in ring:
                    x_values.append(coord[0])
                    y_values.append(coord[1])
        elif geom_type in {"MultiPolygon", "GeometryCollection"}:
            for part in coords:
                self._accumulate_coords(
                    {"type": "Polygon", "coordinates": part}, x_values, y_values
                )
        else:
            for coord in coords:
                if isinstance(coord[0], (list, tuple)):
                    self._accumulate_coords(
                        {"type": "Polygon", "coordinates": [coords]}, x_values, y_values
                    )
                    break
                x_values.append(coord[0])
                y_values.append(coord[1])

    def _copy_asset(
        self,
        href: str,
        target_path: str,
        dtype: str,
        nodata: float | None,
        clip_aoi: bool = False,
    ) -> None:
        # Pass the AOI bounding box when clipping is enabled
        clip_bbox = self.bbox if clip_aoi else None
        future = self.io_client.submit_copy(href, target_path, dtype, nodata, clip_bbox)
        if future is not None:
            future.result()

    def _build_target_path(
        self, asset_dir: str, anonym: str, date_token: str, filename: str
    ) -> str:
        normalized_dir = asset_dir.strip("/ ")
        base = f"abfs://{ADLS_PREFIX}/{normalized_dir}/{anonym}/{date_token}"

        if base.startswith("abfs://"):
            return f"{base}/{filename}"
        return os.path.join(base, filename)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download EO data via Earth Search STAC"
    )
    parser.add_argument(
        "--config",
        default="download.ini",
        help="Path to the download.ini file defining assets",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse config without downloading"
    )
    parser.add_argument(
        "--safe-mode",
        action="store_true",
        help=(
            "Reduce terminal output and disable extra SAR raw/aligned outputs "
            "to avoid crashing browser-based VS Code sessions"
        ),
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="Maximum number of STAC items to process per section (debug/QA)",
    )
    parser.add_argument(
        "--max-assets-per-item",
        type=int,
        default=None,
        help="Maximum number of assets to download per STAC item (debug/QA)",
    )
    parser.add_argument(
        "--skip-uploads",
        action="store_true",
        help="Don't upload outputs to remote storage (keep local temp outputs only)",
    )
    parser.add_argument(
        "--log-path",
        default=None,
        help="Write a detailed run log to this file (recommended in safe mode)",
    )
    parser.add_argument("--aoi", help="Optional override AOI GeoJSON file")
    parser.add_argument(
        "--start-date", help="Optional override start date (YYYY-MM-DD)"
    )
    parser.add_argument("--end-date", help="Optional override end date (YYYY-MM-DD)")

    args = parser.parse_args()

    # Basic logging so RasterDownloader errors show up (useful for the
    # 'truth value of an array is ambiguous' debugging cases).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    downloader = GeoanalyticsDownloader(
        config_path=args.config,
        dry_run=args.dry_run,
        safe_mode=args.safe_mode,
        max_items=args.max_items,
        max_assets_per_item=args.max_assets_per_item,
        skip_uploads=args.skip_uploads,
        log_path=args.log_path,
        aoi_path_override=args.aoi,
        start_date_override=args.start_date,
        end_date_override=args.end_date,
    )
    try:
        downloader.run()
    finally:
        downloader.close()


if __name__ == "__main__":
    main()

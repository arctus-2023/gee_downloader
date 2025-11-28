#!/usr/bin/env python3
"""Download Earth observation scenes from AWS or Planetary Computer STAC endpoints."""

from __future__ import annotations

import argparse
import configparser
import json
import os
from pathlib import Path
from typing import Iterable, List

import adlfs  # registers the `abfs` protocol for fsspec  # noqa: F401
import pendulum
from geoanalytics_io_client import GeoanalyticsIOClient, IOConfig
from pystac_client import Client
from pystac_client.exceptions import APIError

STAC_ENDPOINTS = [
    "https://earth-search.aws.element84.com/v1",
    "https://planetarycomputer.microsoft.com/api/stac/v1",
]

STAC_COLLECTION_MAP = {
    "LC08_L1TOA": "landsat-8-l1",
    "LC08_L2RGB": "landsat-8-l2",
    "LC09_L1TOA": "landsat-9-l1",
    "LC09_L2RGB": "landsat-9-l2",
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
        "SAA": "saa",
        "SZA": "sza",
        "VAA": "vaa",
        "VZA": "vza",
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

ADLS_PREFIX = "01j9ajb2mdvmnkyhpahfevcy2t-sageport-main"


def _safe_split(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _normalize_band_name(name: str) -> str:
    candidate = name.strip().upper().replace("-", "_").replace(" ", "_")

    for prefix in (
        "SR_",
        "ST_",
        "OLI_",
        "TIRS_",
        "L2SP_",
        "L2SR_",
        "L1TP_",
        "L1GT_",
        "L1GS_",
    ):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]

    if candidate.startswith("B") and len(candidate) > 1:
        digits = candidate[1:]
        if digits.isdigit():
            candidate = f"B{int(digits)}"
    return candidate


class GeoanalyticsDownloader:
    def __init__(
        self,
        config_path: str,
        dry_run: bool = False,
        aoi_path_override: str | None = None,
        start_date_override: str | None = None,
        end_date_override: str | None = None,
    ):
        config = configparser.ConfigParser()
        config.read(config_path)
        if "GLOBAL" not in config:
            raise ValueError("download.ini must contain a [GLOBAL] section")

        self.config = config
        self.global_config = config["GLOBAL"]
        self.dry_run = dry_run

        self.aoi_path = aoi_path_override or self.global_config.get("aoi") or ""
        if not self.aoi_path:
            raise ValueError(
                "AOI path must be defined either in GLOBAL section or via --aoi"
            )
        if not Path(self.aoi_path).exists():
            raise FileNotFoundError(f"AOI file not found: {self.aoi_path}")

        self.aoi_name = Path(self.aoi_path).stem
        self.bbox = self._load_aoi_bbox(self.aoi_path)

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
        self.target = self.global_config.get("target", "all")
        self.asset_order = _safe_split(self.global_config.get("assets", ""))
        self.override_map = self._load_overrides()
        io_config = IOConfig(
            adl_account=self.global_config.get("adl_account_name"),
        )
        self.io_client = GeoanalyticsIOClient(io_config)

    def run(self) -> None:
        print("Starting Geoanalytics download workflow")
        try:
            for section in self.asset_order:
                if section not in self.config:
                    print(f"Skipping {section}: configuration missing")
                    continue
                asset_config = self.config[section]
                collection = STAC_COLLECTION_MAP.get(section)
                if collection is None:
                    print(f"No STAC mapping available for {section}; skipping")
                    continue

                include_bands = _safe_split(asset_config.get("include_bands", ""))
                try:
                    resolution = int(asset_config.get("resolution", "0"))
                except ValueError:
                    resolution = 0

                anonym = asset_config.get("anonym", section)
                asset_savedir = asset_config.get("save_dir", "misc")

                for current_date in self._iter_dates():
                    date_str = current_date.format("YYYY-MM-DD")
                    print(f"Processing {section} for {date_str}")
                    if self.dry_run:
                        print(f"  [dry-run] would search {collection} for {date_str}")
                        continue

                    item = self._find_stac_item(collection, current_date)
                    if item is None:
                        print(f"  No STAC item found for {collection} on {date_str}")
                        continue

                    matched_assets = self._match_assets(section, item, include_bands)
                    if not matched_assets:
                        print(f"  No matching assets found for {section} on {date_str}")
                        continue

                    for asset_key in matched_assets:
                        asset = item.assets[asset_key]
                        suffix = Path(asset.href).suffix or ".dat"
                        proposal = asset_key.replace("/", "_")
                        filename = f"{section}_{date_str}_{proposal}_{self.aoi_name}_{resolution}m{suffix}"
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
                            )
                        except Exception as exc:
                            print(f"    Failed to copy {asset.href}: {exc}")
        finally:
            self.io_client.close()

    def _find_stac_item(self, collection: str, date: pendulum.DateTime):
        period = f"{date.format('YYYY-MM-DD')}/{date.add(days=1).format('YYYY-MM-DD')}"
        query = [f"eo:cloud_cover <= {self.cloud_threshold}"]
        for endpoint in STAC_ENDPOINTS:
            try:
                client = Client.open(endpoint)
            except Exception as exc:
                print(f"  Could not open STAC endpoint {endpoint}: {exc}")
                continue

            # Try preferred search (with sorting by eo:cloud_cover). Some
            # collections do not expose that property and the backend will
            # return a BadRequest. In that case we fall back to looser queries.
            try:
                search = client.search(
                    collections=[collection],
                    bbox=self.bbox,
                    datetime=period,
                    query=query,
                    limit=1,
                    sortby=[{"field": "eo:cloud_cover", "direction": "asc"}],
                )
                item = next(iter(search.items()), None)
                if item is not None:
                    return item
            except APIError as exc:
                print(
                    f"  STAC endpoint {endpoint} rejected sort/query: {exc}. Retrying without sort..."
                )
            except Exception as exc:  # unexpected errors
                print(f"  STAC search failed at {endpoint}: {exc}")

            # Fallback 1: try without sort (keep cloud cover filter)
            try:
                search = client.search(
                    collections=[collection],
                    bbox=self.bbox,
                    datetime=period,
                    query=query,
                    limit=1,
                )
                item = next(iter(search.items()), None)
                if item is not None:
                    return item
            except Exception as exc:
                print(f"  STAC fallback (no-sort) failed at {endpoint}: {exc}")

            # Fallback 2: try without query (some indices may not index eo:cloud_cover)
            try:
                search = client.search(
                    collections=[collection],
                    bbox=self.bbox,
                    datetime=period,
                    limit=1,
                )
                item = next(iter(search.items()), None)
                if item is not None:
                    return item
            except Exception as exc:
                print(f"  STAC fallback (no-query) failed at {endpoint}: {exc}")

        return None

    def _iter_dates(self) -> Iterable[pendulum.DateTime]:
        current = self.start_date
        while current <= self.end_date:
            yield current
            current = current.add(days=1)

    def _build_asset_alias_map(self, section: str, item) -> dict[str, List[str]]:
        """Construct a lookup that maps normalized band names to actual asset keys."""
        alias_map: dict[str, List[str]] = {}

        for asset_name, asset in item.assets.items():
            normalized = _normalize_band_name(asset_name)
            alias_map.setdefault(normalized, [])
            if asset_name not in alias_map[normalized]:
                alias_map[normalized].append(asset_name)

            eo_bands = (
                asset.extra_fields.get("eo:bands", [])
                if hasattr(asset, "extra_fields")
                else []
            )
            for band_info in eo_bands:
                if isinstance(band_info, dict):
                    eo_name = band_info.get("name")
                    if eo_name:
                        normalized_eo = _normalize_band_name(eo_name)
                        alias_map.setdefault(normalized_eo, [])
                        if asset_name not in alias_map[normalized_eo]:
                            alias_map[normalized_eo].append(asset_name)
                    common_name = band_info.get("common_name")
                    if common_name:
                        normalized_common = _normalize_band_name(common_name)
                        alias_map.setdefault(normalized_common, [])
                        if asset_name not in alias_map[normalized_common]:
                            alias_map[normalized_common].append(asset_name)

        dataset_mapping = EARTH_SEARCH_ASSET_MAP.get(section, {})
        for source_name, alias in dataset_mapping.items():
            source_norm = _normalize_band_name(source_name)
            alias_norm = _normalize_band_name(alias)

            source_assets = alias_map.get(source_norm, [])
            alias_assets = alias_map.get(alias_norm, [])

            if source_assets and not alias_assets:
                alias_map[alias_norm] = list(source_assets)
            elif alias_assets and not source_assets:
                alias_map[source_norm] = list(alias_assets)
            elif alias_assets and source_assets:
                merged = source_assets + [
                    asset for asset in alias_assets if asset not in source_assets
                ]
                alias_map[source_norm] = merged
                alias_map[alias_norm] = list(merged)

        self._apply_overrides(section, alias_map, item)

        return alias_map

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
            return list(item.assets.keys())

        alias_map = self._build_asset_alias_map(section, item)
        matches: List[str] = []
        seen: set[str] = set()

        for band in include_bands:
            normalized = _normalize_band_name(band)
            for asset_name in alias_map.get(normalized, []):
                if asset_name not in seen:
                    matches.append(asset_name)
                    seen.add(asset_name)

        if matches:
            return matches

        return list(item.assets.keys())

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
        self, href: str, target_path: str, dtype: str, nodata: float | None
    ) -> None:
        future = self.io_client.submit_copy(href, target_path, dtype, nodata)
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
    parser.add_argument("--aoi", help="Optional override AOI GeoJSON file")
    parser.add_argument(
        "--start-date", help="Optional override start date (YYYY-MM-DD)"
    )
    parser.add_argument("--end-date", help="Optional override end date (YYYY-MM-DD)")

    args = parser.parse_args()
    downloader = GeoanalyticsDownloader(
        config_path=args.config,
        dry_run=args.dry_run,
        aoi_path_override=args.aoi,
        start_date_override=args.start_date,
        end_date_override=args.end_date,
    )
    downloader.run()


if __name__ == "__main__":
    main()

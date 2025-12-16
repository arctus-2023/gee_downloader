from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from asset_policy import (  # noqa: E402
    AssetDownloadPolicy,
    build_asset_alias_map,
    normalize_band_name,
)


def test_resolve_upload_raw_sar_global_vs_section_override():
    """Smoke test of the option resolution helper without full downloader init."""

    import configparser

    from geoanalytics_downloader import GeoanalyticsDownloader  # type: ignore

    cfg = configparser.ConfigParser()
    cfg.read_dict(
        {
            "GLOBAL": {"upload_raw_sar": "true"},
            "S1_L1C": {"upload_raw_sar": "false"},
        }
    )

    dl = GeoanalyticsDownloader.__new__(GeoanalyticsDownloader)
    dl.config = cfg
    dl.global_config = cfg["GLOBAL"]
    dl.upload_raw_sar = True

    assert dl._resolve_upload_raw_sar("S1_L1C") is False
    assert dl._resolve_upload_raw_sar("S2_L2SURF") is True


def test_resolve_upload_aligned_raw_sar_global_vs_section_override():
    # Import inside test to avoid importing the module at collection time in
    # minimal environments.
    from geoanalytics_downloader import GeoanalyticsDownloader

    dl = GeoanalyticsDownloader.__new__(GeoanalyticsDownloader)
    dl.config = {
        "GLOBAL": {"upload_aligned_raw_sar": "true"},
        "S1_L1C": {"upload_aligned_raw_sar": "false"},
    }
    dl.global_config = dl.config["GLOBAL"]
    dl.upload_aligned_raw_sar = True

    # section override should win
    assert dl._resolve_upload_aligned_raw_sar("S1_L1C") is False
    # no section override should fall back to global attr
    assert dl._resolve_upload_aligned_raw_sar("S2_L2SURF") is True


class _FakeAsset:
    def __init__(self, extra_fields=None):
        self.extra_fields = extra_fields or {}


def test_normalize_band_name_strips_prefix_and_normalizes_digits():
    assert normalize_band_name("sr_b02") == "B2"
    assert normalize_band_name("B08") == "B8"
    assert normalize_band_name("  l2sr_b11 ") == "B11"


def test_asset_download_policy_skips_jp2_assets():
    policy = AssetDownloadPolicy()
    assert policy.is_supported_asset("S2_L2SURF", "B02-jp2") is False
    assert policy.is_supported_asset("S2_L2SURF", "B02-jpx") is False
    assert policy.is_supported_asset("S2_L2SURF", "B02") is True


def test_asset_download_policy_skips_visual_for_rgb_section():
    policy = AssetDownloadPolicy()
    assert policy.is_supported_asset("LC08_L2RGB", "visual") is False
    assert policy.is_supported_asset("LC08_L2RGB", "SR_B2") is True


def test_build_asset_alias_map_uses_eo_bands_common_names():
    assets = {
        "B02": _FakeAsset(extra_fields={"eo:bands": [{"common_name": "blue"}]}),
        "B03": _FakeAsset(extra_fields={"eo:bands": [{"common_name": "green"}]}),
    }

    alias_map = build_asset_alias_map(
        section="S2_L2SURF",
        item_assets=assets,
        earth_search_asset_map={"S2_L2SURF": {"B02": "blue"}},
        overrides=None,
    )

    assert alias_map["B2"] == ["B02"]
    assert alias_map["BLUE"] == ["B02"]
    assert alias_map["GREEN"] == ["B03"]


def test_build_asset_alias_map_applies_overrides():
    assets = {
        "coastal": _FakeAsset(extra_fields={"eo:bands": [{"name": "B01"}]}),
        "B01": _FakeAsset(extra_fields={}),
    }

    overrides = {"B1": ["coastal"]}

    alias_map = build_asset_alias_map(
        section="S2_L2SURF",
        item_assets=assets,
        earth_search_asset_map={},
        overrides=overrides,
    )

    assert alias_map["B1"] == ["coastal"]


def test_build_asset_alias_map_ignores_unknown_override_targets():
    assets = {"B02": _FakeAsset()}
    overrides = {"B2": ["does-not-exist"]}

    alias_map = build_asset_alias_map(
        section="S2_L2SURF",
        item_assets=assets,
        earth_search_asset_map={},
        overrides=overrides,
    )

    # override doesn't resolve, so it shouldn't wipe the base mapping
    assert alias_map["B2"] == ["B02"]

from __future__ import annotations

from dataclasses import dataclass

import pendulum


@dataclass
class _FakeAsset:
    href: str
    extra_fields: dict


class _FakeItem:
    def __init__(self, item_id: str, assets: dict, *, orbit_state: str | None = None):
        self.id = item_id
        self.assets = assets
        # Minimal properties used by the downloader.
        self.properties = {"sat:orbit_state": orbit_state}


def test_multi_item_sar_passes_reference_to_download(monkeypatch, tmp_path):
    """Regression: multi-item downloads for SAR must pass reference_data and enable
    alignment; otherwise Sentinel-1 assets can be missing/unclippable.

    This is a lightweight unit test that stubs out I/O and checks call arguments.
    """

    try:
        import geoanalytics_downloader as gad
    except Exception:
        # Some environments only ship the CLI script with a hyphenated name.
        # In that case this unit test can't be imported safely.
        import pytest

        pytest.skip("geoanalytics_downloader module not importable")

    GeoanalyticsDownloader = gad.GeoanalyticsDownloader

    # Build a bare instance without running __init__.
    dl = GeoanalyticsDownloader.__new__(GeoanalyticsDownloader)
    dl.bbox = [0.0, 0.0, 1.0, 1.0]
    dl.aoi_name = "aoi"
    dl.temp_download_dir = str(tmp_path)
    dl._emit = lambda msg: None

    # Multi-item function looks these up.
    monkeypatch.setattr(gad, "STAC_COLLECTION_MAP", {"S1_L1C": "sentinel-1-grd"})
    monkeypatch.setattr(
        GeoanalyticsDownloader,
        "_is_sar_collection",
        lambda self, collection: True,
    )

    # Stubbed helpers.
    monkeypatch.setattr(GeoanalyticsDownloader, "_resolve_target_crs", lambda self, section: "EPSG:4326")
    monkeypatch.setattr(GeoanalyticsDownloader, "_match_assets", lambda self, section, item, include: list(item.assets.keys()))
    monkeypatch.setattr(GeoanalyticsDownloader, "_extract_proj_metadata", lambda self, asset, item: {})

    ref_obj = object()
    monkeypatch.setattr(GeoanalyticsDownloader, "_fetch_reference_data", lambda self, **kwargs: ref_obj)

    calls: list[dict] = []

    def _fake_download_to_local(
        self,
        href,
        local_path,
        dtype,
        nodata,
        spatial_metadata=None,
        asset_name=None,
        fallback_crs=None,
        reference_data=None,
        orbit_state=None,
        align_to_reference=False,
        clip_bbox=None,
        target_resolution=None,
    ):
        calls.append(
            {
                "href": href,
                "local_path": local_path,
                "reference_data": reference_data,
                "align_to_reference": align_to_reference,
                "orbit_state": orbit_state,
            }
        )
        # Create a tiny but valid GeoTIFF so the downloader counts it.
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        data = np.zeros((1, 2, 2), dtype=np.uint8)
        profile = {
            "driver": "GTiff",
            "dtype": "uint8",
            "width": 2,
            "height": 2,
            "count": 1,
            "crs": "EPSG:4326",
            "transform": from_origin(0, 1, 0.5, 0.5),
        }
        with rasterio.open(local_path, "w", **profile) as dst:
            dst.write(data)

    monkeypatch.setattr(GeoanalyticsDownloader, "_download_to_local", _fake_download_to_local)

    merged = {"called": False}

    def _fake_merge_to_single_file(**kwargs):
        merged["called"] = True
        assert kwargs["downloaded_files"], "expected at least one downloaded file"

    monkeypatch.setattr(dl, "_merge_to_single_file", _fake_merge_to_single_file)

    items = [
        _FakeItem(
            "it1",
            {"hh": _FakeAsset("s3://bucket/iw-hh.tiff", {"raster:bands": [{}]})},
            orbit_state="ascending",
        ),
        _FakeItem(
            "it2",
            {"hh": _FakeAsset("s3://bucket/iw-hh.tiff", {"raster:bands": [{}]})},
            orbit_state="ascending",
        ),
    ]

    dl._download_and_merge_assets_multi_item(
        section="S1_L1C",
        items=items,
        include_bands=[],
        asset_savedir="x",
        anonym="x",
        current_date=pendulum.datetime(2025, 10, 3),
        resolution=10,
        clip_aoi=False,
    )

    assert merged["called"] is True
    assert calls, "expected download_to_local to be invoked"
    # SAR path must pass the reference and enable alignment.
    assert all(c["reference_data"] is ref_obj for c in calls)
    assert all(c["align_to_reference"] is True for c in calls)

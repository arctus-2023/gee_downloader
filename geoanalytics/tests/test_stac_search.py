from __future__ import annotations

from dataclasses import dataclass

import pytest

from stac_search import CoverageEvaluator, StacSearchConfig, StacSearcher


@dataclass
class _FakeSearch:
    items_list: list

    def items(self):
        yield from self.items_list


class _FakeClient:
    def __init__(self, responses):
        # responses: list[list[item]] for successive search() calls
        self._responses = list(responses)

    def search(self, **kwargs):
        if not self._responses:
            return _FakeSearch([])
        return _FakeSearch(self._responses.pop(0))


class _FakeItem:
    def __init__(self, item_id: str, geometry=None, bbox=None):
        self.id = item_id
        self.geometry = geometry
        self.bbox = bbox


def test_is_sar_collection():
    s = StacSearcher(StacSearchConfig(endpoints=[]))
    assert s.is_sar_collection("sentinel-1-grd") is True
    assert s.is_sar_collection("sentinel-2-l2a") is False


def test_sar_item_selection_prefers_best_coverage(monkeypatch):
    # AOI bbox is 0..10 square.
    aoi = [0, 0, 10, 10]

    # Two geometries: half coverage vs full coverage.
    item_low = _FakeItem(
        "low",
        geometry={
            "type": "Polygon",
            "coordinates": [[[0, 0], [5, 0], [5, 10], [0, 10], [0, 0]]],
        },
    )
    item_high = _FakeItem(
        "high",
        geometry={
            "type": "Polygon",
            "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        },
    )

    # Fake client returns both items.
    fake_client = _FakeClient([[item_low, item_high]])

    s = StacSearcher(StacSearchConfig(endpoints=["fake"]))

    monkeypatch.setattr(s, "open_client", lambda endpoint: fake_client)

    best = s.find_item(
        collection="sentinel-1-grd",
        bbox=aoi,
        period="2020-01-01/2020-01-02",
    )

    assert best.id == "high"


def test_optical_fallback_without_sort(monkeypatch):
    # Simulate APIError on first attempt, then success.
    from pystac_client.exceptions import APIError

    aoi = [0, 0, 10, 10]

    item = _FakeItem("ok")

    class _ClientWithError(_FakeClient):
        def search(self, **kwargs):
            # first call includes sortby
            if "sortby" in kwargs:
                raise APIError("bad request")
            return _FakeSearch([item])

    s = StacSearcher(StacSearchConfig(endpoints=["fake"], cloud_threshold=20))
    monkeypatch.setattr(s, "open_client", lambda endpoint: _ClientWithError([]))

    got = s.find_item(
        collection="sentinel-2-l2a",
        bbox=aoi,
        period="2020-01-01/2020-01-02",
    )

    assert got.id == "ok"


def test_coverage_evaluator_handles_missing_geometry():
    ev = CoverageEvaluator()
    aoi = [0, 0, 10, 10]

    item = _FakeItem("bbox-only", geometry=None, bbox=[0, 0, 10, 10])
    cov = ev.compute_coverage(item, aoi)
    assert cov == pytest.approx(1.0)

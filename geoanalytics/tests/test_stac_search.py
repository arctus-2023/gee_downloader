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
    def __init__(self, item_id: str, geometry=None, bbox=None, properties=None):
        self.id = item_id
        self.geometry = geometry
        self.bbox = bbox
        self.properties = properties or {}


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


def test_find_items_optical_returns_multiple(monkeypatch):
    aoi = [0, 0, 10, 10]
    item1 = _FakeItem("a", properties={"datetime": "2020-01-01T10:00:00Z"})
    item2 = _FakeItem("b", properties={"datetime": "2020-01-01T12:00:00Z"})

    fake_client = _FakeClient([[item1, item2]])

    s = StacSearcher(StacSearchConfig(endpoints=["fake"], cloud_threshold=20))
    monkeypatch.setattr(s, "open_client", lambda endpoint: fake_client)

    items = s.find_items(
        collection="sentinel-2-l2a",
        bbox=aoi,
        period="2020-01-01/2020-01-02",
        limit=10,
    )

    assert [it.id for it in items] == ["a", "b"]


def test_find_items_filters_to_requested_day(monkeypatch):
    aoi = [0, 0, 10, 10]
    item_day1 = _FakeItem("day1", properties={"datetime": "2020-01-01T23:59:59Z"})
    item_day2 = _FakeItem("day2", properties={"datetime": "2020-01-02T00:00:00Z"})

    fake_client = _FakeClient([[item_day1, item_day2]])
    s = StacSearcher(StacSearchConfig(endpoints=["fake"]))
    monkeypatch.setattr(s, "open_client", lambda endpoint: fake_client)

    items = s.find_items(
        collection="sentinel-2-l2a",
        bbox=aoi,
        period="2020-01-01/2020-01-02",
        limit=10,
    )

    assert [it.id for it in items] == ["day1"]


def test_find_items_sar_does_not_filter_to_day(monkeypatch):
    """SAR keeps historical behavior: do not apply strict UTC day filtering."""
    aoi = [0, 0, 10, 10]
    item_day1 = _FakeItem("day1", properties={"datetime": "2020-01-01T23:59:59Z"})
    item_day2 = _FakeItem("day2", properties={"datetime": "2020-01-02T00:00:00Z"})

    fake_client = _FakeClient([[item_day1, item_day2]])
    s = StacSearcher(StacSearchConfig(endpoints=["fake"]))
    monkeypatch.setattr(s, "open_client", lambda endpoint: fake_client)

    items = s.find_items(
        collection="sentinel-1-grd",
        bbox=aoi,
        period="2020-01-01/2020-01-02",
        limit=10,
    )

    assert {it.id for it in items} == {"day1", "day2"}


def test_find_items_optical_day_tolerance_keeps_adjacent_day(monkeypatch):
    aoi = [0, 0, 10, 10]
    item_day1 = _FakeItem("day1", properties={"datetime": "2020-01-01T23:59:59Z"})
    item_day2 = _FakeItem("day2", properties={"datetime": "2020-01-02T00:00:00Z"})

    fake_client = _FakeClient([[item_day1, item_day2]])
    s = StacSearcher(StacSearchConfig(endpoints=["fake"]))
    monkeypatch.setattr(s, "open_client", lambda endpoint: fake_client)

    items = s.find_items(
        collection="sentinel-2-l2a",
        bbox=aoi,
        period="2020-01-01/2020-01-02",
        limit=10,
        day_tolerance=1,
    )

    assert {it.id for it in items} == {"day1", "day2"}


def test_find_items_sar_orders_by_coverage(monkeypatch):
    aoi = [0, 0, 10, 10]

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

    fake_client = _FakeClient([[item_low, item_high]])
    s = StacSearcher(StacSearchConfig(endpoints=["fake"]))
    monkeypatch.setattr(s, "open_client", lambda endpoint: fake_client)

    items = s.find_items(
        collection="sentinel-1-grd",
        bbox=aoi,
        period="2020-01-01/2020-01-02",
        limit=10,
    )

    assert [it.id for it in items] == ["high", "low"]

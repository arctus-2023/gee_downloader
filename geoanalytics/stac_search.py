"""STAC item discovery for Geoanalytics.

This module encapsulates the complexity of querying multiple STAC endpoints and
picking the "best" item for a given day.

The downloader historically mixed:
- endpoint resolution
- Planetary Computer signing
- optical vs SAR logic
- per-endpoint fallbacks

Moving those concerns here makes the orchestration layer simpler and testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from pystac_client import Client
from pystac_client.exceptions import APIError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StacSearchConfig:
    endpoints: Sequence[str]
    cloud_threshold: float = 100.0


class StacSearcher:
    def __init__(self, config: StacSearchConfig):
        self.config = config

    def open_client(self, endpoint: str) -> Optional[Client]:
        kwargs: Dict[str, Any] = {}
        if "planetarycomputer.microsoft.com" in endpoint:
            try:
                import planetary_computer

                kwargs["modifier"] = planetary_computer.sign_inplace
            except ImportError:
                logger.warning(
                    "planetary_computer package is not installed; requests to Planetary Computer will be unsigned and may fail"
                )

        try:
            return Client.open(endpoint, **kwargs)
        except Exception as exc:
            logger.warning("Could not open STAC endpoint %s: %s", endpoint, exc)
            return None

    def is_sar_collection(self, collection: str) -> bool:
        sar_collections = {
            "sentinel-1-grd",
            "sentinel-1-rtc",
            "sentinel-1-slc",
            "cop-dem-glo-30",
            "cop-dem-glo-90",
        }
        return collection.lower() in sar_collections

    def iter_endpoints(
        self, endpoints: Optional[Sequence[str]] = None
    ) -> Iterable[str]:
        for ep in endpoints or self.config.endpoints:
            yield ep

    def find_item(
        self,
        *,
        collection: str,
        bbox: List[float],
        period: str,
        endpoints: Optional[Sequence[str]] = None,
        coverage_evaluator: Optional["CoverageEvaluator"] = None,
    ):
        """Find a STAC item for a collection/period/bbox.

        If collection is SAR, optional `coverage_evaluator` is used to select the
        item with best AOI coverage.
        """

        if self.is_sar_collection(collection):
            evaluator = coverage_evaluator or CoverageEvaluator()
            return self._find_item_sar(collection, bbox, period, endpoints, evaluator)

        return self._find_item_optical(collection, bbox, period, endpoints)

    def _find_item_optical(
        self,
        collection: str,
        bbox: List[float],
        period: str,
        endpoints: Optional[Sequence[str]],
    ):
        query = [f"eo:cloud_cover <= {self.config.cloud_threshold}"]

        for endpoint in self.iter_endpoints(endpoints):
            client = self.open_client(endpoint)
            if client is None:
                continue

            try:
                search = client.search(
                    collections=[collection],
                    bbox=bbox,
                    datetime=period,
                    query=query,
                    limit=1,
                    sortby=[{"field": "eo:cloud_cover", "direction": "asc"}],
                )
                item = next(iter(search.items()), None)
                if item is not None:
                    return item
            except APIError as exc:
                logger.info(
                    "STAC endpoint %s rejected sort/query: %s. Retrying without sort...",
                    endpoint,
                    exc,
                )
            except Exception as exc:
                logger.warning("STAC search failed at %s: %s", endpoint, exc)

            # fallback 1: no sort
            try:
                search = client.search(
                    collections=[collection],
                    bbox=bbox,
                    datetime=period,
                    query=query,
                    limit=1,
                )
                item = next(iter(search.items()), None)
                if item is not None:
                    return item
            except Exception as exc:
                logger.warning(
                    "STAC fallback (no-sort) failed at %s: %s", endpoint, exc
                )

            # fallback 2: no query
            try:
                search = client.search(
                    collections=[collection],
                    bbox=bbox,
                    datetime=period,
                    limit=1,
                )
                item = next(iter(search.items()), None)
                if item is not None:
                    return item
            except Exception as exc:
                logger.warning(
                    "STAC fallback (no-query) failed at %s: %s", endpoint, exc
                )

        return None

    def _find_item_sar(
        self,
        collection: str,
        bbox: List[float],
        period: str,
        endpoints: Optional[Sequence[str]],
        evaluator: "CoverageEvaluator",
    ):
        min_coverage_threshold = 0.5

        for endpoint in self.iter_endpoints(endpoints):
            client = self.open_client(endpoint)
            if client is None:
                continue

            try:
                search = client.search(
                    collections=[collection],
                    bbox=bbox,
                    datetime=period,
                    limit=10,
                )
                items = list(search.items())
                if not items:
                    continue

                best_item = None
                best_cov = 0.0
                for item in items:
                    cov = evaluator.compute_coverage(item, bbox)
                    if cov > best_cov:
                        best_cov = cov
                        best_item = item

                if best_item is None:
                    continue

                if best_cov < min_coverage_threshold:
                    logger.warning(
                        "Best SAR item only covers %.1f%% of AOI (threshold: %.0f%%)",
                        best_cov * 100.0,
                        min_coverage_threshold * 100.0,
                    )

                return best_item

            except Exception as exc:
                logger.warning("SAR STAC search failed at %s: %s", endpoint, exc)

        return None


class CoverageEvaluator:
    """Policy object for ranking candidate SAR items by AOI coverage."""

    def compute_coverage(self, item, aoi_bbox: List[float]) -> float:
        try:
            from shapely.geometry import box, shape

            aoi_polygon = box(aoi_bbox[0], aoi_bbox[1], aoi_bbox[2], aoi_bbox[3])

            item_geom = getattr(item, "geometry", None)
            if item_geom is None:
                item_bbox = getattr(item, "bbox", None)
                if item_bbox:
                    item_polygon = box(*item_bbox)
                else:
                    return 0.0
            else:
                item_polygon = shape(item_geom)

            intersection = aoi_polygon.intersection(item_polygon)
            if intersection.is_empty:
                return 0.0

            return min(intersection.area / aoi_polygon.area, 1.0)
        except Exception:
            return 0.0

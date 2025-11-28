"""
Script to query AWS Earth Search and isolate STAC Item and Asset information.
"""

from pystac_client import Client


def earth_search_collections() -> list[str]:
    """Retrieve the list of available collections from AWS Earth Search.

    Returns:
        list[str]: A list of collection IDs available in Earth Search.
    """
    search_client = Client.open("https://earth-search.aws.element84.com/v1")
    collections = search_client.get_all_collections()
    collection_ids = [collection.id for collection in collections]
    return collection_ids


def query_earth_search(
    collection: str,
    date: str,
    platform: str | None = None,
    bbox: list[float] | None = None,
    intersects: dict | None = None,
) -> list:
    """Query AWS Earth Search for STAC Items matching the specified criteria.

    Args:
        collection (str): The collection to query (e.g., 'sentinel-2-l2a-cogs').
        date (str): The date or date range to query (e.g., '2023-01-01' or '2023-01-01/2023-01-31').
        bbox (list[float] | None): Optional bounding box [minX, minY, maxX, maxY].
        intersects (dict | None): Optional GeoJSON geometry for spatial filtering.

    Returns:
        list: A list of STAC Items matching the query.
    """
    search_client = Client.open("https://earth-search.aws.element84.com/v1")

    platform_filter = platform
    if platform_filter is None and collection.lower().startswith("landsat"):
        platform_filter = "LANDSAT_08"

    search = search_client.search(
        collections=[collection],
        datetime=date,
        bbox=bbox,
        intersects=intersects,
        query={"platform": platform_filter} if platform_filter else None,
    )
    items = list(search.items())
    return items


# Print Item JSON for debugging
if __name__ == "__main__":
    import json

    collections = earth_search_collections()
    print("Available Collections in Earth Search:")
    for coll in collections:
        print(f" - {coll}")

    items = query_earth_search(
        collection="landsat-c2-l2",
        platform="LANDSAT_8",
        date="2023-01-01/2023-01-31",
        bbox=[-10.0, 35.0, 0.0, 45.0],
    )
    for item in items[:1]:
        print(json.dumps(item.to_dict(), indent=2))

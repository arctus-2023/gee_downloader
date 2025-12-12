import os
import requests
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon
from datetime import datetime
from pathlib import Path
import pandas as pd

# ----------------------------
# CONFIGURATION
# ----------------------------
CDSE_USERNAME = os.environ.get("CDSE_USERNAME", "tj@arctus.ca")
CDSE_PASSWORD = os.environ.get("CDSE_PASSWORD", "kY38CFpz@s%$Qkw")

# AOI GeoJSON (from your example)
AOI_PATH = "/media/thomas/Arctus_data2/0_Arctus_Project/NRCAN_Lake_Meliadine/AOI/Meliadine_Lake.geojson"

# Date range (UTC)
START_DATE = "2024-08-17T00:00:00.000Z"
END_DATE   = "2024-08-19T23:59:59.999Z"

# Cloud cover max (%)
MAX_CLOUD = 10

# Sentinel-2 Level:
#   L1C -> 'S2MSI1C'
#   L2A -> 'S2MSI2A'
PRODUCT_TYPE = "S2MSI1C"

# Output directory
OUT_DIR = Path("./S2_CDSE_L1C_Meliadine")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ----------------------------
# 1. Get CDSE access token
# ----------------------------

def get_access_token(username: str, password: str) -> str:
    """
    Get OAuth2 access token for Copernicus Data Space Ecosystem.
    """
    token_url = (
        "https://identity.dataspace.copernicus.eu/"
        "auth/realms/CDSE/protocol/openid-connect/token"
    )
    data = {
        "client_id": "cdse-public",
        "grant_type": "password",
        "username": username,
        "password": password,
    }

    r = requests.post(token_url, data=data)
    r.raise_for_status()
    return r.json()["access_token"]


# ----------------------------
# 2. Read AOI and build POLYGON WKT
# ----------------------------

def geojson_to_polygon_wkt(path: str) -> str:
    """
    Read a GeoJSON and return a POLYGON WKT string suitable for
    OData.CSC.Intersects(...POLYGON(...)).
    If geometry is a MultiPolygon, take the first polygon.
    """
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise ValueError("AOI GeoJSON has no features")

    geom = gdf.geometry.iloc[0]

    # CDSE OData (like CREODIAS) does not support MULTIPOLYGON in Intersects,
    # so we convert to a single polygon if necessary.
    if isinstance(geom, MultiPolygon):
        # take the largest polygon by area (or just first one)
        geom = max(geom.geoms, key=lambda p: p.area)

    if not isinstance(geom, Polygon):
        raise ValueError(f"Unsupported geometry type: {type(geom)}")

    # Ensure closed ring (first == last) – shapely guarantees it, but we enforce
    coords = list(geom.exterior.coords)
    if coords[0] != coords[-1]:
        coords.append(coords[0])
        geom = Polygon(coords)

    # Return WKT like: POLYGON((lon lat, lon lat, ...))
    return geom.wkt


# ----------------------------
# 3. Query CDSE OData catalogue
# ----------------------------

def search_s2_products(token: str, polygon_wkt: str) -> pd.DataFrame:
    """
    Search Sentinel-2 products over AOI with date + cloud constraints.
    Returns a pandas DataFrame with product metadata.
    """
    base_url = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

    # IMPORTANT:
    #  - Use Collection/Name eq 'SENTINEL-2' to select the sensor family
    #  - Use Attributes/any(...) only for attributes that genuinely exist:
    #      * productType  (StringAttribute)
    #      * cloudCover   (DoubleAttribute)
    #  - Date fields are ContentDate/Start and ContentDate/End (DateTimeOffset)
    #
    odata_filter = (
        f"Collection/Name eq 'SENTINEL-2' "
        f"and OData.CSC.Intersects(area=geography'SRID=4326;{polygon_wkt}') "
        f"and ContentDate/Start ge {START_DATE} "
        f"and ContentDate/Start le {END_DATE} "
        f"and Attributes/any(a:a/Name eq 'cloudCover' "
        f"    and a/OData.CSC.DoubleAttribute/Value le {MAX_CLOUD}) "
        f"and Attributes/any(a:a/Name eq 'productType' "
        f"    and a/OData.CSC.StringAttribute/Value eq '{PRODUCT_TYPE}')"
    )

    headers = {
        "Authorization": f"Bearer {token}",
    }

    params = {
        "$filter": odata_filter,
        "$format": "json",
        "$top": 100,                      # adjust if needed
        "$orderby": "ContentDate/Start asc",
    }

    r = requests.get(base_url, headers=headers, params=params)
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        # Helpful debug if it still fails
        print("CDSE catalogue error:", r.text)
        raise

    data = r.json().get("value", [])

    if not data:
        print("No products found for given criteria.")
        return pd.DataFrame()

    df = pd.DataFrame.from_dict(data)
    return df



# ----------------------------
# 4. Download products (ZIP -> SAFE inside)
# ----------------------------

def download_product_zip(token: str, product_id: str, name: str, out_dir: Path) -> Path:
    """
    Download a single product ZIP using its OData Id.
    """
    # Recommended for file download: zipper endpoint
    # You can also use catalogue endpoint, but zipper is designed for data download.
    download_url = f"https://zipper.dataspace.copernicus.eu/odata/v1/Products({product_id})/$value"

    headers = {
        "Authorization": f"Bearer {token}",
    }

    out_path = out_dir / f"{name}.zip"
    print(f"Downloading {name} -> {out_path}")

    with requests.get(download_url, headers=headers, stream=True) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    return out_path


# ----------------------------
# MAIN
# ----------------------------

if __name__ == "__main__":
    # 1) Token
    token = get_access_token(CDSE_USERNAME, CDSE_PASSWORD)
    print("Access token acquired.")

    # 2) AOI WKT
    polygon_wkt = geojson_to_polygon_wkt(AOI_PATH)
    print("AOI polygon WKT ready.")

    # 3) Search products
    df = search_s2_products(token, polygon_wkt)
    if df.empty:
        print("No Sentinel-2 products found. Exiting.")
        raise SystemExit

    # Show some info
    cols = ["Id", "Name", "ContentDate", "OriginDate", "GeoFootprint"]
    print(df[cols].head())

    # 4) Download all found products
    for _, row in df.iterrows():
        product_id = row["Id"]
        product_name = row["Name"]
        download_product_zip(token, product_id, product_name, OUT_DIR)

    print("All downloads completed.")

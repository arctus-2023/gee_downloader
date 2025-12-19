# Zarr Output Format Guide

This guide explains how to use Zarr as an output format for merged Earth observation data in the Geoanalytics Downloader.

## What is Zarr?

[Zarr](https://zarr.dev/) is a cloud-native data format designed for storing large N-dimensional arrays. It offers several advantages for geospatial data:

- **Chunked storage**: Data is stored in chunks that can be read independently
- **Cloud-optimized**: Works seamlessly with cloud object storage (Azure Blob, S3, GCS)
- **Parallel access**: Multiple processes can read/write different chunks simultaneously
- **Compression**: Built-in support for various compression algorithms
- **Metadata**: Stores rich metadata alongside the data

## When to Use Zarr vs COG

| Feature | Zarr | Cloud Optimized GeoTIFF (COG) |
|---------|------|------------------------------|
| **Best for** | Analytics, ML workflows | Traditional GIS, visualization |
| **Parallel read/write** | ✅ Excellent | ⚠️ Read-only parallel |
| **GIS software support** | ⚠️ Limited (Python/R) | ✅ Universal |
| **Multi-dimensional data** | ✅ Native support | ⚠️ Workarounds needed |
| **Append operations** | ✅ Supported | ❌ Not supported |
| **Streaming writes** | ✅ Supported | ⚠️ Limited |

**Choose Zarr when:**
- Building ML/analytics pipelines in Python
- Working with large datasets that need parallel processing
- Building time series or multi-dimensional data cubes
- Writing data incrementally/streaming

**Choose COG when:**
- Sharing with GIS software users (QGIS, ArcGIS)
- Maximum compatibility is required
- Working with web mapping services

## Configuration

### Basic Zarr Output

In your `geoanalytics-download.ini`:

```ini
[GLOBAL]
merge_outputs = true
output_format = zarr

# Optional: customize chunk sizes (bands, height, width)
zarr_chunks = 1,512,512
```

### Per-Section Override

You can use different formats for different datasets:

```ini
[GLOBAL]
output_format = cog  # Default to COG

[S2_L1TOA]
merge_outputs = true
output_format = zarr  # Use Zarr for Sentinel-2 L1

[LC08_L1TOA]
merge_outputs = true
output_format = cog   # Use COG for Landsat
```

### Chunk Size Tuning

Chunks control how data is partitioned. The format is `bands,height,width`:

```ini
# Default: good balance for most use cases
zarr_chunks = 1,512,512

# Larger chunks: better for sequential reads, less overhead
zarr_chunks = 1,1024,1024

# Smaller chunks: better for random access, higher overhead
zarr_chunks = 1,256,256

# All bands in one chunk: useful for band math operations
zarr_chunks = 13,512,512
```

## Reading Zarr Data in Python

### Using xarray (Recommended)

```python
import xarray as xr

# Open from local path
ds = xr.open_zarr("path/to/output.zarr")

# Open from Azure Blob Storage
import adlfs
ds = xr.open_zarr(
    "abfs://container/path/to/output.zarr",
    storage_options={"account_name": "your_account"}
)

# Access the data
print(ds)
print(ds.data)  # The main raster data
print(ds.band)  # Band names
print(ds.x)     # X coordinates
print(ds.y)     # Y coordinates

# Access metadata
print(ds.attrs["crs"])
print(ds.attrs["transform"])
print(ds.attrs["bandnames"])
```

### Using zarr directly

```python
import zarr
import fsspec

# Open from Azure
fs = fsspec.filesystem("abfs", account_name="your_account")
store = fs.get_mapper("container/path/to/output.zarr")
root = zarr.open_group(store, mode="r")

# Access data
print(root.info)
data = root["data"][:]  # Load all data
band_0 = root["data"][0, :, :]  # Load just band 0

# Access metadata
print(root.attrs["crs"])
print(root.attrs["bandnames"])
```

### Lazy Loading with Dask

```python
import xarray as xr

# Open with Dask for lazy loading
ds = xr.open_zarr(
    "path/to/output.zarr",
    chunks={"band": 1, "y": 512, "x": 512}
)

# Operations are lazy - no data loaded yet
ndvi = (ds.data.sel(band="B08") - ds.data.sel(band="B04")) / \
       (ds.data.sel(band="B08") + ds.data.sel(band="B04"))

# Compute only when needed
result = ndvi.compute()
```

## Writing Zarr Programmatically

The `ZarrDatasetWriter` class supports streaming writes:

```python
from geoanalytics.utils import ZarrDatasetWriter

# Initialize writer
writer = ZarrDatasetWriter(
    output_path="abfs://container/output.zarr",
    io_client=io_client,
)

# Open with schema
writer.open(
    shape=(4, 1000, 1000),  # 4 bands, 1000x1000 pixels
    chunks=(1, 512, 512),
    dtype="float32",
    crs="EPSG:32610",
    transform=my_affine_transform,
    bandnames=["B02", "B03", "B04", "B08"],
)

# Write bands incrementally
for i, band_data in enumerate(downloaded_bands):
    writer.write_band(i, band_data)

# Close and finalize
writer.close()
```

Or use the convenience function:

```python
from geoanalytics.utils import write_raster_to_zarr

write_raster_to_zarr(
    data=my_3d_array,
    output_path="abfs://container/output.zarr",
    io_client=io_client,
    crs="EPSG:32610",
    transform=my_affine_transform,
    bandnames=["B02", "B03", "B04"],
)
```

## Zarr Dataset Structure

The output Zarr datasets have this structure:

```
output.zarr/
├── .zgroup           # Group metadata
├── .zattrs           # Attributes (CRS, transform, etc.)
├── data/             # Main data array (bands, y, x)
│   ├── .zarray
│   └── 0.0.0, 0.0.1, ...  # Chunk files
├── band/             # Band names coordinate
│   └── .zarray
├── x/                # X coordinates
│   └── .zarray
└── y/                # Y coordinates
    └── .zarray
```

### Attributes Stored

| Attribute | Description | Example |
|-----------|-------------|---------|
| `crs` | Coordinate Reference System | `"EPSG:32610"` |
| `transform` | Affine transform coefficients | `[10.0, 0.0, 500000.0, 0.0, -10.0, 5000000.0]` |
| `bandnames` | List of band names | `["B02", "B03", "B04"]` |
| `nodata` | NoData value | `0` or `null` |
| `descriptions` | Dataset description | `"S2_L1TOA:2025-01-01:..."` |
| `cloud_percentage` | Cloud cover percentage | `15.5` |

## Best Practices

1. **Choose appropriate chunk sizes**: Match your access patterns
   - Reading full images? Use larger chunks (1024x1024)
   - Random tile access? Use smaller chunks (256x256)

2. **Use consolidated metadata**: The writer consolidates metadata by default for faster opens

3. **Leverage parallel processing**: Zarr excels when you use Dask or multiprocessing

4. **Consider compression**: Zarr uses Blosc with zstd by default, which is fast and efficient

5. **Use xarray for analysis**: xarray provides the best high-level interface for Zarr geospatial data

## Troubleshooting

### "No module named 'zarr'"
```bash
pip install zarr
```

### "Unable to open store"
Check your storage credentials and path format:
```python
# Azure Blob Storage
"abfs://container/path/to/file.zarr"

# S3
"s3://bucket/path/to/file.zarr"

# Local
"/path/to/file.zarr"
```

### "Chunks are too small/large"
Adjust `zarr_chunks` in your config. Typical good values:
- `1,512,512` - Balanced (default)
- `1,256,256` - Small, for random access
- `1,1024,1024` - Large, for sequential processing

## Example Workflow

```ini
# geoanalytics-download.ini
[GLOBAL]
aoi = ./my_area.geojson
start_date = 2025-01-01
end_date = 2025-01-31
assets = S2_L2SURF
merge_outputs = true
output_format = zarr
zarr_chunks = 1,512,512

[S2_L2SURF]
include_bands = B02,B03,B04,B08
resolution = 10
save_dir = sentinel2
anonym = s2_msi
```

```python
# analysis.py
import xarray as xr

# Open the downloaded data
ds = xr.open_zarr("abfs://mycontainer/sentinel2/s2_msi/20250101/S2_L2SURF_2025-01-01_my_area_10m_merged.zarr")

# Calculate NDVI
nir = ds.data.sel(band="B08")
red = ds.data.sel(band="B04")
ndvi = (nir - red) / (nir + red)

# Save result
ndvi.to_zarr("abfs://mycontainer/results/ndvi_january.zarr")
```

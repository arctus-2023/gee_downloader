import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import utils  # noqa: E402


def _write_test_tif(
    path: Path, data: np.ndarray, transform, crs: str = "EPSG:4326"
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=data.dtype,
        crs=crs,
        transform=transform,
    ) as dst:
        dst.write(data, 1)


def test_stack_bands_resamples_to_target_resolution(tmp_path):
    transform = from_origin(0, 20, 20, 20)
    band1 = tmp_path / "band1.tif"
    band2 = tmp_path / "band2.tif"

    _write_test_tif(band1, np.ones((2, 2), dtype=np.uint16), transform)
    _write_test_tif(band2, np.ones((2, 2), dtype=np.uint16) * 2, transform)

    stacked, out_transform, dst_crs, metadata = utils.stack_bands(
        [str(band1), str(band2)], target_resolution=10
    )

    assert stacked.shape == (2, 4, 4)
    assert pytest.approx(out_transform.a) == 10
    assert dst_crs is not None
    assert metadata["bounds"] is not None


def test_stack_bands_to_xarray_respects_resolution(tmp_path):
    transform = from_origin(0, 40, 20, 20)
    band1 = tmp_path / "band1.tif"
    band2 = tmp_path / "band2.tif"

    _write_test_tif(band1, np.ones((2, 2), dtype=np.uint16), transform)
    _write_test_tif(band2, np.ones((2, 2), dtype=np.uint16) * 3, transform)

    ds = utils.stack_bands_to_xarray(
        [str(band1), str(band2)],
        bandnames=["one", "two"],
        target_resolution=5,
    )

    assert list(ds.attrs.get("bandnames", [])) == ["one", "two"]
    assert ds["data"].shape[0] == 2
    # 20m pixels -> 4x upsample for 5m target => 8x8 grid
    assert ds["data"].shape[1:] == (8, 8)
    resx, resy = ds["data"].rio.resolution()
    assert pytest.approx(resx) == 5
    assert pytest.approx(abs(resy)) == 5

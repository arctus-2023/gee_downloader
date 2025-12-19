from __future__ import annotations

from raster_io import DimensionNormalizer, SpatialMetadataApplier


class _FakeRio:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height


class _FakeDataset:
    def __init__(self, width: int, height: int, dims):
        self.rio = _FakeRio(width, height)
        self.dims = tuple(dims)
        self.transpose_calls = []

    def transpose(self, *dims):
        self.transpose_calls.append(dims)
        # Return self to keep it simple
        return self


class _FakeDataArray(_FakeDataset):
    def __init__(self, dims):
        # width/height aren't used by DimensionNormalizer
        super().__init__(width=1, height=1, dims=dims)

    def rename(self, mapping):
        new_dims = [mapping.get(d, d) for d in self.dims]
        self.dims = tuple(new_dims)
        return self


def test_compute_transform_from_bbox_uses_actual_dimensions():
    metadata = {
        "proj:bbox": [0.0, 0.0, 20.0, 10.0],
    }

    # actual width=4, height=2 => xres=5, yres=-5
    transform = SpatialMetadataApplier._compute_transform_from_bbox(metadata, 4, 2)
    assert transform == (5.0, 0.0, 0.0, 0.0, -5.0, 10.0)


def test_normalize_grid_orientation_noop_when_shape_matches():
    ds = _FakeDataset(width=10, height=20, dims=("band", "y", "x"))
    md = {"proj:shape": [20, 10]}
    out = SpatialMetadataApplier._normalize_grid_orientation(ds, md, asset_label="x")
    assert out is ds
    assert ds.transpose_calls == []


def test_normalize_grid_orientation_transposes_when_swapped():
    ds = _FakeDataset(width=20, height=10, dims=("band", "y", "x"))
    md = {"proj:shape": [20, 10]}
    out = SpatialMetadataApplier._normalize_grid_orientation(ds, md, asset_label="x")
    assert out is ds
    # expects transpose(..., "x", "y")
    assert ds.transpose_calls == [("band", "x", "y")]


def test_dimension_normalizer_reorders_band_xy():
    arr = _FakeDataArray(dims=("band", "x", "y"))
    out = DimensionNormalizer().normalize(arr, asset_label="x")
    assert out is arr
    assert arr.transpose_calls == [("band", "y", "x")]


def test_dimension_normalizer_renames_time_to_band_and_orders():
    arr = _FakeDataArray(dims=("time", "y", "x"))
    out = DimensionNormalizer().normalize(arr, asset_label="x")
    assert out is arr
    # rename should have changed dims to ('band','y','x'), no transpose needed
    assert arr.dims == ("band", "y", "x")

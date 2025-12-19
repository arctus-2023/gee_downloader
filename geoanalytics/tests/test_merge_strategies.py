from __future__ import annotations

import pytest
from merge_strategies import build_merge_strategy


def test_build_merge_strategy_cog():
    s = build_merge_strategy(output_format="cog", hierarchical=False)
    assert s.__class__.__name__ == "CogMergeStrategy"


def test_build_merge_strategy_flat_zarr():
    s = build_merge_strategy(output_format="zarr", hierarchical=False)
    assert s.__class__.__name__ == "FlatZarrMergeStrategy"


def test_build_merge_strategy_hierarchical_requires_spec():
    with pytest.raises(ValueError):
        build_merge_strategy(output_format="zarr", hierarchical=True)

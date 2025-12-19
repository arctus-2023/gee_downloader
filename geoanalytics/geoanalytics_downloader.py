"""Import shim for `geoanalytics-downloader.py`.

The historical entrypoint script is named `geoanalytics-downloader.py` (with a
hyphen), which can't be imported as a normal Python module.

Our tests (and downstream callers) expect to be able to do:

    from geoanalytics_downloader import GeoanalyticsDownloader

This shim loads the hyphenated script dynamically and re-exports the symbols we
need.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_script_module() -> ModuleType:
    here = Path(__file__).resolve().parent
    script_path = here / "geoanalytics-downloader.py"
    if not script_path.exists():
        raise FileNotFoundError(f"Expected downloader script at {script_path}")

    spec = importlib.util.spec_from_file_location("geoanalytics_downloader_script", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed creating spec for {script_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_script: ModuleType = _load_script_module()

# Re-export the public API used by tests.
GeoanalyticsDownloader: Any = getattr(_script, "GeoanalyticsDownloader")

__all__ = ["GeoanalyticsDownloader"]

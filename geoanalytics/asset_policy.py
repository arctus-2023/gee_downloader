"""Asset selection and filtering policy for Geoanalytics STAC downloads.

This module is intentionally small and testable. It centralizes:
- band name normalization (for config overrides and EO band matching)
- rules for skipping unsupported assets

The goal is to keep `geoanalytics-downloader.py` focused on orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence


def safe_split(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def normalize_band_name(name: str) -> str:
    """Normalize a band/config name into a canonical token.

    Examples:
    - "sr_b02" -> "B2"
    - "B08" -> "B8"
    - "red-edge1" -> "RED_EDGE1" (punctuation normalized)

    Note: This is a *token* normalization, not a mapping to STAC asset keys.
    """

    candidate = name.strip().upper().replace("-", "_").replace(" ", "_")

    for prefix in (
        "SR_",
        "ST_",
        "OLI_",
        "TIRS_",
        "L2SP_",
        "L2SR_",
        "L1TP_",
        "L1GT_",
        "L1GS_",
    ):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]

    if candidate.startswith("B") and len(candidate) > 1:
        digits = candidate[1:]
        if digits.isdigit():
            candidate = f"B{int(digits)}"

    return candidate


@dataclass(frozen=True)
class AssetDownloadPolicy:
    """Rules for which STAC assets should be downloaded.

    The old downloader code had these checks duplicated in multiple methods.
    Centralizing them reduces branching and prevents inconsistent behavior.
    """

    skip_suffixes: tuple[str, ...] = ("-jp2", "-jpx")

    def is_supported_asset(self, section: str, asset_key: str) -> bool:
        """Return True if the asset key should be considered for download."""
        key_lower = asset_key.lower()

        # STAC assets that point to JPEG2000 are not supported by this workflow.
        if key_lower.endswith(self.skip_suffixes):
            return False

        # RGB "visual" assets are not supported for download/merge.
        # Historically: skip when section implies RGB product; allow pipeline to
        # handle optical+band assets.
        if "RGB" in section.upper() and "visual" in key_lower:
            return False

        return True

    def filter_assets(self, section: str, asset_keys: Sequence[str]) -> List[str]:
        return [k for k in asset_keys if self.is_supported_asset(section, k)]


def build_asset_alias_map(
    *,
    section: str,
    item_assets: Mapping[str, object],
    earth_search_asset_map: Dict[str, Dict[str, str]],
    overrides: Dict[str, Dict[str, List[str]]] | None = None,
) -> Dict[str, List[str]]:
    """Build a lookup from normalized band tokens -> STAC asset keys.

    `item_assets` should be `item.assets` from pystac.

    This is pulled out so it can be unit tested without STAC I/O.
    """

    alias_map: Dict[str, List[str]] = {}

    # 1) direct mapping from key names + eo:bands common names
    for asset_name, asset in item_assets.items():
        normalized = normalize_band_name(asset_name)
        alias_map.setdefault(normalized, [])
        if asset_name not in alias_map[normalized]:
            alias_map[normalized].append(asset_name)

        extra_fields = getattr(asset, "extra_fields", None)
        eo_bands = (
            extra_fields.get("eo:bands", []) if isinstance(extra_fields, dict) else []
        )
        for band_info in eo_bands:
            if not isinstance(band_info, dict):
                continue

            for key in ("name", "common_name"):
                eo_name = band_info.get(key)
                if not eo_name:
                    continue
                normalized_eo = normalize_band_name(str(eo_name))
                alias_map.setdefault(normalized_eo, [])
                if asset_name not in alias_map[normalized_eo]:
                    alias_map[normalized_eo].append(asset_name)

    # 2) merge known dataset mappings (e.g. B02 -> blue)
    dataset_mapping = earth_search_asset_map.get(section, {})
    for source_name, alias in dataset_mapping.items():
        source_norm = normalize_band_name(source_name)
        alias_norm = normalize_band_name(alias)

        source_assets = alias_map.get(source_norm, [])
        alias_assets = alias_map.get(alias_norm, [])

        if source_assets and not alias_assets:
            alias_map[alias_norm] = list(source_assets)
        elif alias_assets and not source_assets:
            alias_map[source_norm] = list(alias_assets)
        elif alias_assets and source_assets:
            merged = source_assets + [a for a in alias_assets if a not in source_assets]
            alias_map[source_norm] = merged
            alias_map[alias_norm] = list(merged)

    # 3) apply overrides
    if overrides:
        for band_name, preferred_assets in overrides.items():
            resolved: List[str] = []
            for candidate in preferred_assets:
                asset_key = candidate.strip()
                if not asset_key:
                    continue
                if asset_key in item_assets:
                    if asset_key not in resolved:
                        resolved.append(asset_key)
                    continue

                normalized_candidate = normalize_band_name(asset_key)
                for fallback in alias_map.get(normalized_candidate, []):
                    if fallback not in resolved:
                        resolved.append(fallback)

            if resolved:
                alias_map[band_name] = resolved

    return alias_map

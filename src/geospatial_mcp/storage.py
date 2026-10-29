"""Read-only, operator-managed dataset catalog and bounded local file access."""

import json
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import ValidationError

from .errors import ConfigurationError, GeoError
from .schemas import DatasetCatalog
from .settings import Settings

ALLOWED_SUFFIXES = {"vector": {".geojson", ".json", ".gpkg", ".shp"}, "raster": {".tif", ".tiff"}}
SHAPEFILE_SIDECARS = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix"}


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _relative_path(raw: str) -> Path:
    parts = raw.replace("\\", "/").split("/")
    if (
        PureWindowsPath(raw).drive
        or raw.startswith(("/", "\\"))
        or "\x00" in raw
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
    ):
        raise ValueError("Catalog paths must be ordinary relative paths without traversal.")
    return Path(*parts)


class DatasetStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        try:
            if settings.catalog_path.stat().st_size > 1024 * 1024:
                raise ValueError("Catalog too large")
            raw = json.loads(settings.catalog_path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys)
            self.catalog = DatasetCatalog.model_validate(raw)
            self.paths = {key: _relative_path(entry.path) for key, entry in self.catalog.datasets.items()}
        except (OSError, ValueError, ValidationError) as exc:
            raise ConfigurationError("Invalid dataset catalog: check schema, unique IDs and relative paths (max 1 MiB).") from exc

    def _checked_file(self, path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self.settings.data_root):
                raise GeoError("PATH_NOT_ALLOWED", "Dataset resolves outside the configured data root.")
            if not resolved.is_file():
                raise GeoError("DATASET_UNREADABLE", "Dataset is not a regular file.")
            return resolved
        except (OSError, RuntimeError) as exc:
            raise GeoError("DATASET_UNREADABLE", "Dataset is missing or cannot be read.") from exc

    def resolve_readable(self, dataset_id: str, kind: Literal["vector", "raster"]) -> Path:
        entry = self.catalog.datasets.get(dataset_id)
        if entry is None:
            raise GeoError("DATASET_NOT_FOUND", "Unknown dataset ID; consult the operator's dataset catalog.")
        if entry.kind != kind:
            raise GeoError("DATASET_TYPE_MISMATCH", f"This tool requires a {kind} dataset.")
        requested = self.settings.data_root / self.paths[dataset_id]
        path = self._checked_file(requested)
        if requested.suffix.lower() not in ALLOWED_SUFFIXES[kind] or path.suffix.lower() not in ALLOWED_SUFFIXES[kind]:
            raise GeoError("UNSUPPORTED_FORMAT", "This dataset format is not enabled in phase A.")
        files = [path]
        if path.suffix.lower() == ".shp":
            # Check companion files too: a safe .shp must not lead to an external .dbf/.prj.
            files = [
                self._checked_file(sibling)
                for sibling in path.parent.iterdir()
                if sibling.stem.lower() == path.stem.lower()
                and sibling.suffix.lower() in SHAPEFILE_SIDECARS
            ]
        try:
            total_bytes = sum(file.stat().st_size for file in files)
        except OSError as exc:
            raise GeoError("DATASET_UNREADABLE", "Dataset cannot be read.") from exc
        if total_bytes > self.settings.max_input_bytes:
            raise GeoError("INPUT_TOO_LARGE", "Dataset exceeds GEO_MAX_INPUT_BYTES.")
        return path

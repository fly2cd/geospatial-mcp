"""Configuration is read only at startup, never from the benchmark config or .env."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError


@dataclass(frozen=True)
class Settings:
    data_root: Path
    catalog_path: Path
    max_input_bytes: int = 256 * 1024 * 1024
    max_vector_features: int = 1_000_000
    log_level: str = "INFO"
    workspace_root: Path | None = None
    max_analysis_features: int = 100_000
    max_output_features: int = 100_000
    max_artifact_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("data_root", "catalog_path"):
            path = Path(getattr(self, name))
            if not path.is_absolute():
                raise ConfigurationError(f"{name} must be an absolute path.")
            object.__setattr__(self, name, path.resolve())
        if not self.data_root.is_dir():
            raise ConfigurationError("GEO_DATA_ROOT must be an existing directory.")
        if not self.catalog_path.is_file():
            raise ConfigurationError("Dataset catalog must be an existing JSON file.")
        if self.max_input_bytes <= 0 or self.max_vector_features <= 0:
            raise ConfigurationError("Input byte and vector feature limits must be positive.")
        if min(self.max_analysis_features, self.max_output_features, self.max_artifact_bytes) <= 0:
            raise ConfigurationError("Analysis and artifact limits must be positive.")
        if self.workspace_root is not None:
            workspace = Path(self.workspace_root)
            if not workspace.is_absolute():
                raise ConfigurationError("GEO_WORKSPACE_ROOT must be absolute.")
            workspace = workspace.resolve()
            if workspace.is_relative_to(self.data_root) or self.data_root.is_relative_to(workspace):
                raise ConfigurationError("Data root and workspace root must not overlap.")
            if workspace.exists() and not workspace.is_dir():
                raise ConfigurationError("GEO_WORKSPACE_ROOT must be a directory.")
            object.__setattr__(self, "workspace_root", workspace)
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("GEO_LOG_LEVEL is not a supported log level.")

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if env is None else env
        root = values.get("GEO_DATA_ROOT", "")
        if not root:
            raise ConfigurationError("GEO_DATA_ROOT is required; use an absolute data directory.")
        try:
            return cls(
                data_root=Path(root),
                catalog_path=Path(values.get("GEO_DATA_CATALOG", str(Path(root) / "datasets.json"))),
                max_input_bytes=int(values.get("GEO_MAX_INPUT_BYTES", str(256 * 1024 * 1024))),
                max_vector_features=int(values.get("GEO_MAX_VECTOR_FEATURES", "1000000")),
                log_level=values.get("GEO_LOG_LEVEL", "INFO").upper(),
                workspace_root=Path(values["GEO_WORKSPACE_ROOT"]) if values.get("GEO_WORKSPACE_ROOT") else None,
                max_analysis_features=int(values.get("GEO_MAX_ANALYSIS_FEATURES", "100000")),
                max_output_features=int(values.get("GEO_MAX_OUTPUT_FEATURES", "100000")),
                max_artifact_bytes=int(values.get("GEO_MAX_ARTIFACT_BYTES", str(16 * 1024 * 1024))),
            )
        except (ValueError, OSError) as exc:
            if isinstance(exc, ConfigurationError):
                raise
            raise ConfigurationError("Invalid geospatial service configuration.") from exc

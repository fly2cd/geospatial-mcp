"""JSON-safe tool contracts, independent of the MCP implementation."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

DatasetId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Bounds = tuple[float, float, float, float]
CrsType = Literal["geographic", "projected", "other", "undefined"]
NoData = float | Literal["NaN", "+Infinity", "-Infinity"] | None


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class FieldInfo(Contract):
    name: str
    data_type: str


class VectorInfo(Contract):
    kind: Literal["vector"] = "vector"
    driver: str
    layer: str
    available_layers: list[str]
    feature_count: int = Field(ge=0)
    geometry_types: list[str]
    crs: str | None
    crs_type: CrsType
    coordinate_units: list[str]
    bounds: Bounds | None = Field(description="xmin, ymin, xmax, ymax in source CRS units; null for empty data")
    fields: list[FieldInfo]
    null_geometries: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class RasterBand(Contract):
    band: int = Field(ge=1)
    dtype: str
    nodata: NoData
    description: str | None
    unit: str | None


class RasterInfo(Contract):
    kind: Literal["raster"] = "raster"
    driver: str
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    band_count: int = Field(ge=1)
    crs: str | None
    crs_type: CrsType
    coordinate_units: list[str]
    bounds: Bounds
    transform: tuple[float, float, float, float, float, float] = Field(description="Affine a,b,c,d,e,f")
    pixel_size: tuple[float, float] = Field(description="Pixel column/row vector lengths in source CRS units")
    pixel_size_meters: tuple[float, float] | None
    bands: list[RasterBand]
    warnings: list[str] = Field(default_factory=list)


class DatasetEntry(Contract):
    path: str = Field(min_length=1)
    kind: Literal["vector", "raster"]


class DatasetCatalog(Contract):
    version: Literal[1] = 1
    datasets: dict[DatasetId, DatasetEntry]


# Stage B: identifiers are opaque references, not file paths or access tokens.
TaskId = Annotated[str, Field(pattern=r"^task_[a-f0-9]{32}$")]
ArtifactId = Annotated[str, Field(pattern=r"^art_[a-f0-9]{32}$")]
LayerName = Annotated[str, Field(min_length=1, max_length=255)]


class SourceRef(Contract):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, json_schema_extra={
        "oneOf": [
            {"required": ["dataset_id"], "properties": {"dataset_id": {"type": "string"}, "artifact_id": {"type": "null"}}},
            {"required": ["artifact_id"], "properties": {"artifact_id": {"type": "string"}, "dataset_id": {"type": "null"}}},
        ]
    })
    dataset_id: DatasetId | None = None
    artifact_id: ArtifactId | None = None
    layer: LayerName | None = None

    @model_validator(mode="after")
    def exactly_one_reference(self):
        if (self.dataset_id is None) == (self.artifact_id is None):
            raise ValueError("Supply exactly one of dataset_id or artifact_id.")
        return self


class DatasetDescription(Contract):
    dataset_id: DatasetId
    kind: Literal["vector", "raster"]


class DatasetList(Contract):
    datasets: list[DatasetDescription]


class InputDigest(Contract):
    source: SourceRef
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)


class ArtifactInfo(Contract):
    artifact_id: ArtifactId
    task_id: TaskId
    kind: Literal["vector", "image"]
    format: Literal["gpkg", "png"]
    mime_type: Literal["application/geopackage+sqlite3", "image/png"]
    uri: str
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    crs: str | None = None
    layer: str | None = None
    feature_count: int | None = Field(default=None, ge=0)


class TaskResult(Contract):
    task_id: TaskId
    artifacts: list[ArtifactInfo]
    summary: dict[str, JsonValue]
    warnings: list[str] = Field(default_factory=list)


class TaskInfo(Contract):
    task_id: TaskId
    tool: str
    version: str
    status: Literal["running", "succeeded", "failed"]
    started_at: str
    finished_at: str | None = None
    parameters: dict[str, JsonValue]
    inputs: list[InputDigest] = Field(default_factory=list)
    artifacts: list[ArtifactInfo] = Field(default_factory=list)
    summary: dict[str, JsonValue] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None

"""Explicit stage B tools and an actual binary MCP resource delivery endpoint."""

from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.resources import ResourceContent, ResourceResult
from pydantic import Field

from ..errors import GeoError
from ..operations import AnalysisService
from ..schemas import ArtifactId, ArtifactInfo, DatasetDescription, DatasetList, SourceRef, TaskId, TaskInfo, TaskResult
from ..storage import DatasetStore
from ..workspace import TaskStore

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
CREATES_ARTIFACT = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}


def _call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except GeoError as exc:
        raise ToolError(str(exc)) from None


def register_analysis_tools(mcp: FastMCP, datasets: DatasetStore, tasks: TaskStore) -> None:
    service = AnalysisService(datasets, tasks)

    @mcp.tool(annotations=READ_ONLY)
    def geo_list_datasets() -> DatasetList:
        """List operator-registered dataset IDs and kinds, without exposing server paths."""
        return DatasetList(datasets=[DatasetDescription(dataset_id=key, kind=value.kind) for key, value in sorted(datasets.catalog.datasets.items())])

    @mcp.tool(annotations=READ_ONLY)
    def geo_get_task_info(task_id: TaskId) -> TaskInfo:
        """Read persisted task status, input hashes, parameters, warnings and output metadata.

        Tasks are synchronous in stage B; a process interrupted mid-task may leave running status.
        IDs do not provide multi-user authorization: this service is single-operator only.
        """
        return _call(tasks.get, task_id)

    @mcp.tool(annotations=READ_ONLY)
    def geo_get_artifact_info(artifact_id: ArtifactId) -> ArtifactInfo:
        """Get verified artifact metadata. Use resources/read on its URI to retrieve the actual file bytes."""
        return _call(tasks.artifact, artifact_id)[0]

    @mcp.tool(annotations=CREATES_ARTIFACT)
    def geo_vector_buffer(source: SourceRef,
                          distance_meters: Annotated[float, Field(gt=0, le=1_000_000)],
                          analysis_crs: Annotated[str | None, Field(pattern=r"^EPSG:[1-9][0-9]{2,5}$")] = None) -> TaskResult:
        """Create a GPKG planar buffer, retaining attributes, in a projected analysis CRS.

        source selects exactly one dataset_id or vector artifact_id; layer is optional.
        Geographic sources require an explicit projected analysis_crs. Coordinates are genuinely
        transformed, units converted, and source coordinates checked against the CRS area of use.
        Distance is on the projection plane, not geodesic ground distance; choose a suitable CRS.
        No implicit geometry repair; output CRS is the analysis CRS. Creates a new isolated task.
        """
        return _call(service.buffer, source, distance_meters, analysis_crs)

    @mcp.tool(annotations=CREATES_ARTIFACT)
    def geo_vector_clip(source: SourceRef, mask: SourceRef) -> TaskResult:
        """Intersect source features with the union of polygon masks; write GPKG retaining source attributes.

        Both CRSs must exist and match; no implicit CRS relabeling/reprojection. Boundary-only and
        mixed-dimension intersections are retained. An empty result is a valid empty GPKG layer.
        """
        return _call(service.clip, source, mask)

    @mcp.tool(annotations=CREATES_ARTIFACT)
    def geo_spatial_join(left: SourceRef, right: SourceRef,
                         predicate: Literal["intersects", "within", "contains"] = "intersects",
                         how: Literal["left", "inner"] = "left") -> TaskResult:
        """One-to-many planar spatial join, preserving left geometry; matching CRSs required.

        Predicates are evaluated as left geometry <predicate> right geometry. Output attributes
        use left_/right_ prefixes and __left_row/__right_row (zero-based source order).
        Unmatched left rows have null right attributes. No nearest, right join or aggregation.
        """
        return _call(service.join, left, right, predicate, how)

    @mcp.tool(annotations=CREATES_ARTIFACT)
    def geo_render_vector_map(source: SourceRef,
                              value_field: Annotated[str | None, Field(min_length=1, max_length=255)] = None,
                              title: Annotated[str, Field(max_length=120, pattern=r"^[ -~]*$")] = "Vector map",
                              width: Annotated[int, Field(ge=480, le=2000)] = 1000,
                              height: Annotated[int, Field(ge=360, le=2000)] = 700) -> TaskResult:
        """Render a PNG vector map with legend, source CRS and coordinate bounds.

        An optional numeric field uses a sequential color ramp; null/non-numeric values are rejected.
        No basemap/network requests. Bundled-font titles are ASCII only. Polygon holes are preserved.
        The image is available as real PNG bytes via resources/read, not just a server file path.
        """
        return _call(service.render, source, value_field, title, width, height)

    @mcp.resource("artifact://{artifact_id}", name="geospatial_artifact",
                  description="Read a completed GPKG or PNG artifact as bounded, checksum-verified binary content.")
    def read_artifact(artifact_id: str) -> ResourceResult:
        try:
            info, content = tasks.read(artifact_id)
            return ResourceResult([ResourceContent(content, mime_type=info.mime_type)])
        except GeoError as exc:
            raise ResourceError(str(exc)) from None

"""Application orchestration: bounded inputs -> isolated task -> published artifact."""

from collections.abc import Callable

from .errors import GeoError
from .schemas import InputDigest, SourceRef, TaskInfo, TaskResult
from .storage import DatasetStore
from .workspace import TaskStore, digest
from .services.vector import buffer_vector, clip_vector, load_vector, spatial_join, write_vector
from .services.cartography import render_vector_map


class AnalysisService:
    def __init__(self, datasets: DatasetStore, tasks: TaskStore):
        self.datasets = datasets
        self.tasks = tasks
        self.settings = tasks.settings

    def _load(self, reference: SourceRef, task: TaskInfo):
        layer = reference.layer
        if reference.dataset_id is not None:
            path = self.datasets.resolve_readable(reference.dataset_id, "vector")
        else:
            info, path = self.tasks.artifact(reference.artifact_id)
            if info.kind != "vector":
                raise GeoError("ARTIFACT_TYPE_MISMATCH", "This operation requires a vector artifact, not an image.")
            layer = layer or info.layer
        size = path.stat().st_size
        if size > self.settings.max_input_bytes:
            raise GeoError("INPUT_TOO_LARGE", "Input exceeds GEO_MAX_INPUT_BYTES.")
        task.inputs.append(InputDigest(source=reference, sha256=digest(path), size_bytes=size))
        self.tasks.save(task)
        return load_vector(path, layer=layer, max_features=min(self.settings.max_analysis_features, self.settings.max_vector_features))

    def _run(self, name: str, parameters: dict, format: str, work: Callable) -> TaskResult:
        task = self.tasks.start(name, parameters)
        try:
            summary, warnings = work(task, self.tasks.staging_path(task, format))
            return self.tasks.finish(task, format=format, summary=summary, warnings=warnings)
        except Exception as exc:
            code = exc.code if isinstance(exc, GeoError) else "ANALYSIS_FAILED"
            try:
                self.tasks.fail(task, code)
            except Exception:
                # Do not turn a task failure into a successful response if storage fails too.
                raise GeoError("TASK_PERSISTENCE_FAILED", f"Unable to finalize failure metadata; task_id={task.task_id}.") from None
            message = str(exc).removeprefix(f"[{code}] ") if isinstance(exc, GeoError) else "Analysis failed; check input format and configured workspace."
            raise GeoError(code, f"{message} task_id={task.task_id}") from None

    def buffer(self, source: SourceRef, distance_meters: float, analysis_crs: str | None = None) -> TaskResult:
        parameters = {"source": source.model_dump(exclude_none=True), "distance_meters": distance_meters, "analysis_crs": analysis_crs}
        def work(task, output):
            source_data = self._load(source, task)
            data, warnings = buffer_vector(source_data, distance_meters, analysis_crs=analysis_crs)
            write_vector(data, output, max_features=self.settings.max_output_features)
            return {"feature_count": len(data.rows), "input_feature_count": len(source_data.rows),
                    "crs": data.crs.to_string(), "distance_meters": distance_meters,
                    "distance_crs_units": distance_meters / data.crs.axis_info[0].unit_conversion_factor,
                    "quad_segs": 16}, warnings
        return self._run("geo_vector_buffer", parameters, "gpkg", work)

    def clip(self, source: SourceRef, mask: SourceRef) -> TaskResult:
        parameters = {"source": source.model_dump(exclude_none=True), "mask": mask.model_dump(exclude_none=True)}
        def work(task, output):
            source_data, mask_data = self._load(source, task), self._load(mask, task)
            data = clip_vector(source_data, mask_data)
            write_vector(data, output, max_features=self.settings.max_output_features)
            warnings = ["EMPTY_RESULT: clip produced a valid empty vector layer."] if not data.rows else []
            return {"feature_count": len(data.rows), "input_feature_count": len(source_data.rows), "crs": data.crs.to_string()}, warnings
        return self._run("geo_vector_clip", parameters, "gpkg", work)

    def join(self, left: SourceRef, right: SourceRef, predicate="intersects", how="left") -> TaskResult:
        parameters = {"left": left.model_dump(exclude_none=True), "right": right.model_dump(exclude_none=True), "predicate": predicate, "how": how}
        def work(task, output):
            left_data, right_data = self._load(left, task), self._load(right, task)
            data = spatial_join(left_data, right_data, predicate=predicate, how=how, max_features=self.settings.max_output_features)
            write_vector(data, output, max_features=self.settings.max_output_features)
            return {"feature_count": len(data.rows), "left_feature_count": len(left_data.rows), "right_feature_count": len(right_data.rows),
                    "crs": data.crs.to_string(), "predicate": predicate, "how": how}, []
        return self._run("geo_spatial_join", parameters, "gpkg", work)

    def render(self, source: SourceRef, value_field: str | None = None, title="Vector map", width=1000, height=700) -> TaskResult:
        parameters = {"source": source.model_dump(exclude_none=True), "value_field": value_field, "title": title, "width": width, "height": height}
        def work(task, output):
            data = self._load(source, task)
            return render_vector_map(data, output, value_field=value_field, title=title, width=width, height=height)
        return self._run("geo_render_vector_map", parameters, "png", work)

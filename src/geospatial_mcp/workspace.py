"""Persistent single-operator tasks and content-verified artifacts; no MCP imports."""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from . import __version__
from .errors import ConfigurationError, GeoError
from .schemas import ArtifactInfo, TaskInfo, TaskResult
from .settings import Settings
from .storage import _unique_keys


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class TaskStore:
    def __init__(self, settings: Settings):
        if settings.workspace_root is None:
            raise ConfigurationError("GEO_WORKSPACE_ROOT is required for analysis tools.")
        self.settings = settings
        self.root = settings.workspace_root
        self.root.mkdir(parents=True, exist_ok=True)
        self._check(self.root / "tasks", must_exist=False).mkdir(exist_ok=True)

    def _check(self, path: Path, *, must_exist=True) -> Path:
        try:
            result = path.resolve(strict=must_exist)
            if not result.is_relative_to(self.root):
                raise GeoError("PATH_NOT_ALLOWED", "Workspace entry resolves outside the configured root.")
            return result
        except (OSError, RuntimeError) as exc:
            raise GeoError("TASK_NOT_FOUND", "Task or artifact is unavailable.") from exc

    def directory(self, task_id: str) -> Path:
        if not re.fullmatch(r"task_[a-f0-9]{32}", task_id):
            raise GeoError("INVALID_TASK_ID", "Invalid task identifier.")
        path = self._check(self.root / "tasks" / task_id)
        if not path.is_dir():
            raise GeoError("TASK_NOT_FOUND", "Task is unavailable.")
        return path

    def save(self, task: TaskInfo) -> None:
        directory = self.directory(task.task_id)
        destination = self._check(directory / "manifest.json", must_exist=False)
        temporary = self._check(directory / f"manifest-{uuid4().hex}.tmp", must_exist=False)
        data = task.model_dump_json(indent=2).encode("utf-8")
        if len(data) > 1024 * 1024:
            raise GeoError("MANIFEST_TOO_LARGE", "Task metadata exceeds the manifest limit.")
        with temporary.open("xb") as output:
            output.write(data)
        os.replace(temporary, destination)

    def start(self, tool: str, parameters: dict) -> TaskInfo:
        parent = self._check(self.root / "tasks")
        task_id = "task_" + uuid4().hex
        directory = self._check(parent / task_id, must_exist=False)
        directory.mkdir(exist_ok=False)
        (directory / "intermediate").mkdir()
        (directory / "outputs").mkdir()
        task = TaskInfo(task_id=task_id, tool=tool, version=__version__, status="running", started_at=now(), parameters=parameters)
        self.save(task)
        return task

    def get(self, task_id: str) -> TaskInfo:
        path = self._check(self.directory(task_id) / "manifest.json")
        try:
            with path.open("rb") as stream:
                data = stream.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("Manifest too large")
            result = TaskInfo.model_validate(json.loads(data, object_pairs_hook=_unique_keys))
            if result.task_id != task_id:
                raise ValueError("Mismatched task ID")
            return result
        except (OSError, ValueError, ValidationError) as exc:
            raise GeoError("INVALID_MANIFEST", "Task metadata is invalid.") from exc

    def staging_path(self, task: TaskInfo, format: str) -> Path:
        if format not in {"gpkg", "png"}:
            raise ValueError("Unsupported artifact format")
        return self._check(self.directory(task.task_id) / "intermediate" / ("result." + format), must_exist=False)

    def finish(self, task: TaskInfo, *, format: str, summary: dict, warnings: list[str]) -> TaskResult:
        staged = self._check(self.staging_path(task, format))
        size = staged.stat().st_size
        if size <= 0 or size > self.settings.max_artifact_bytes:
            raise GeoError("ARTIFACT_TOO_LARGE", "Result exceeds GEO_MAX_ARTIFACT_BYTES (or is empty).")
        artifact_id = "art_" + task.task_id.removeprefix("task_")
        info = ArtifactInfo(
            artifact_id=artifact_id, task_id=task.task_id, kind="vector" if format == "gpkg" else "image",
            format=format, mime_type="application/geopackage+sqlite3" if format == "gpkg" else "image/png",
            uri=f"artifact://{artifact_id}", size_bytes=size, sha256=digest(staged),
            crs=summary.get("crs"), layer="result" if format == "gpkg" else None,
            feature_count=summary.get("feature_count"),
        )
        destination = self._check(self.directory(task.task_id) / "outputs" / ("result." + format), must_exist=False)
        if destination.exists():
            raise GeoError("OUTPUT_EXISTS", "Task output already exists; it will not be overwritten.")
        os.replace(staged, destination)
        task.status = "succeeded"
        task.finished_at = now()
        task.artifacts = [info]
        task.summary = summary
        task.warnings = warnings
        self.save(task)
        return TaskResult(task_id=task.task_id, artifacts=[info], summary=summary, warnings=warnings)

    def fail(self, task: TaskInfo, code: str) -> None:
        task.status = "failed"
        task.finished_at = now()
        task.error_code = code
        task.artifacts = []  # Partial files are never advertised/read as artifacts.
        self.save(task)

    def artifact(self, artifact_id: str) -> tuple[ArtifactInfo, Path]:
        if not re.fullmatch(r"art_[a-f0-9]{32}", artifact_id):
            raise GeoError("INVALID_ARTIFACT_ID", "Invalid artifact identifier.")
        task_id = "task_" + artifact_id.removeprefix("art_")
        task = self.get(task_id)
        if task.status != "succeeded" or len(task.artifacts) != 1:
            raise GeoError("ARTIFACT_NOT_READY", "No successful artifact is available for this task.")
        info = task.artifacts[0]
        if info.artifact_id != artifact_id or info.task_id != task_id or info.uri != f"artifact://{artifact_id}":
            raise GeoError("INVALID_MANIFEST", "Artifact metadata is inconsistent.")
        path = self._check(self.directory(task_id) / "outputs" / ("result." + info.format))
        if not path.is_file():
            raise GeoError("ARTIFACT_UNREADABLE", "Artifact is not a regular file.")
        size = path.stat().st_size
        if size > self.settings.max_artifact_bytes:
            raise GeoError("ARTIFACT_TOO_LARGE", "Artifact exceeds the configured resource limit.")
        if size != info.size_bytes or digest(path) != info.sha256:
            raise GeoError("ARTIFACT_INTEGRITY_ERROR", "Artifact content changed after task completion.")
        return info, path

    def read(self, artifact_id: str) -> tuple[ArtifactInfo, bytes]:
        info, path = self.artifact(artifact_id)
        with path.open("rb") as stream:
            content = stream.read(self.settings.max_artifact_bytes + 1)
        if len(content) != info.size_bytes or hashlib.sha256(content).hexdigest() != info.sha256:
            raise GeoError("ARTIFACT_INTEGRITY_ERROR", "Artifact changed while being read.")
        return info, content

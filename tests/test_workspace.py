import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import unittest

from pydantic import ValidationError

from analysis_fixtures import make_analysis_fixture
from geospatial_mcp.errors import ConfigurationError, GeoError
from geospatial_mcp.operations import AnalysisService
from geospatial_mcp.schemas import SourceRef
from geospatial_mcp.settings import Settings
from geospatial_mcp.storage import DatasetStore
from geospatial_mcp.workspace import TaskStore


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        data = self.root / "data"
        make_analysis_fixture(data)
        self.settings = Settings(data, data / "datasets.json", workspace_root=self.root / "workspace")
        self.tasks = TaskStore(self.settings)
        self.datasets = DatasetStore(self.settings)
        self.service = AnalysisService(self.datasets, self.tasks)

    def test_workspace_opt_in_and_nonoverlap(self):
        old = Settings.from_environment({"GEO_DATA_ROOT": str(self.settings.data_root)})
        self.assertIsNone(old.workspace_root)
        for path in ["relative", self.settings.data_root, self.root, self.settings.data_root / "outputs"]:
            with self.subTest(path=path), self.assertRaises(ConfigurationError):
                replace(self.settings, workspace_root=path)
        for field in ["max_analysis_features", "max_output_features", "max_artifact_bytes"]:
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                replace(self.settings, **{field: 0})
        settings = Settings.from_environment({"GEO_DATA_ROOT": str(self.settings.data_root), "GEO_WORKSPACE_ROOT": str(self.root / "another"), "GEO_MAX_ARTIFACT_BYTES": "1000"})
        self.assertEqual(settings.max_artifact_bytes, 1000)

    def test_source_ref_requires_exactly_one_id(self):
        for data in [{}, {"dataset_id": "points", "artifact_id": "art_" + "a" * 32},
                     {"dataset_id": "../points"}, {"artifact_id": "../../secret"}, {"dataset_id": "points", "path": "/tmp/file"}]:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                SourceRef.model_validate(data)

    def test_concurrent_tasks_unique_and_input_not_overwritten(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.service.buffer(SourceRef(dataset_id="points"), 10), range(4)))
        self.assertEqual(len({result.task_id for result in results}), 4)
        self.assertEqual(len({result.artifacts[0].artifact_id for result in results}), 4)
        for result in results:
            task = self.tasks.get(result.task_id)
            self.assertEqual(task.status, "succeeded")
            info, content = self.tasks.read(result.artifacts[0].artifact_id)
            self.assertEqual(hashlib.sha256(content).hexdigest(), info.sha256)

    def test_restart_preserves_results_and_real_bytes(self):
        result = self.service.buffer(SourceRef(dataset_id="points"), 10)
        restarted = TaskStore(self.settings)
        info, content = restarted.read(result.artifacts[0].artifact_id)
        self.assertEqual(restarted.get(result.task_id).summary["feature_count"], 3)
        self.assertTrue(content.startswith(b"SQLite format 3"))
        self.assertEqual(len(content), info.size_bytes)

    def test_tampered_artifact_is_rejected(self):
        result = self.service.buffer(SourceRef(dataset_id="points"), 10)
        _, path = self.tasks.artifact(result.artifacts[0].artifact_id)
        with path.open("r+b") as stream:
            stream.write(b"modified")
        with self.assertRaisesRegex(GeoError, "ARTIFACT_INTEGRITY_ERROR"):
            self.tasks.read(result.artifacts[0].artifact_id)

    def test_reduced_download_limit_is_enforced(self):
        result = self.service.buffer(SourceRef(dataset_id="points"), 10)
        small = TaskStore(replace(self.settings, max_artifact_bytes=10))
        with self.assertRaisesRegex(GeoError, "ARTIFACT_TOO_LARGE"):
            small.read(result.artifacts[0].artifact_id)

    def test_unknown_pathlike_and_running_artifacts_rejected(self):
        for value in ["../../x", "file:///tmp/x", "art_../x"]:
            with self.subTest(value=value), self.assertRaisesRegex(GeoError, "INVALID_ARTIFACT_ID"):
                self.tasks.read(value)
        task = self.tasks.start("test", {})
        with self.assertRaisesRegex(GeoError, "ARTIFACT_NOT_READY"):
            self.tasks.read("art_" + task.task_id.removeprefix("task_"))
        with self.assertRaisesRegex(GeoError, "TASK_NOT_FOUND"):
            self.tasks.get("task_" + "f" * 32)

    def test_malformed_or_oversized_manifest_rejected(self):
        task = self.tasks.start("test", {})
        manifest = self.tasks.directory(task.task_id) / "manifest.json"
        for content in ['{"task_id":"x","task_id":"y"}', ' ' * (1024 * 1024 + 1)]:
            manifest.write_text(content)
            with self.assertRaisesRegex(GeoError, "INVALID_MANIFEST"):
                self.tasks.get(task.task_id)

    def test_workspace_junction_cannot_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        task_id = "task_" + "a" * 32
        link = self.settings.workspace_root / "tasks" / task_id
        try:
            link.symlink_to(outside, target_is_directory=True)
            self.addCleanup(link.unlink)
        except OSError:
            if os.name != "nt":
                self.skipTest("OS disallows symlink creation")
            env = dict(os.environ, GEO_TEST_LINK=str(link), GEO_TEST_TARGET=str(outside))
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:GEO_TEST_LINK -Target $env:GEO_TEST_TARGET | Out-Null"], env=env, capture_output=True, timeout=15)
            if result.returncode:
                self.skipTest("OS disallows junction creation")
            self.addCleanup(link.rmdir)
        with self.assertRaisesRegex(GeoError, "PATH_NOT_ALLOWED"):
            self.tasks.get(task_id)

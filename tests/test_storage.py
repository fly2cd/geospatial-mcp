import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fixtures import make_fixture, make_vector, write_catalog
from geospatial_mcp.errors import ConfigurationError, GeoError
from geospatial_mcp.settings import Settings
from geospatial_mcp.storage import DatasetStore


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.root = self.parent / "data"
        self.entries = make_fixture(self.root)
        self.settings = Settings(data_root=self.root, catalog_path=self.root / "datasets.json")

    def test_registered_path(self):
        self.assertEqual(DatasetStore(self.settings).resolve_readable("roads", "vector"), self.root / "roads.geojson")

    def test_unknown_and_wrong_type(self):
        store = DatasetStore(self.settings)
        for name, kind, code in [("unknown", "vector", "DATASET_NOT_FOUND"), ("dem", "vector", "DATASET_TYPE_MISMATCH")]:
            with self.subTest(code=code), self.assertRaisesRegex(GeoError, code):
                store.resolve_readable(name, kind)

    def test_missing_file(self):
        self.entries["missing"] = {"path": "missing.geojson", "kind": "vector"}
        write_catalog(self.root, self.entries)
        with self.assertRaisesRegex(GeoError, "DATASET_UNREADABLE"):
            DatasetStore(self.settings).resolve_readable("missing", "vector")

    def test_input_byte_limit(self):
        store = DatasetStore(replace(self.settings, max_input_bytes=1))
        with self.assertRaisesRegex(GeoError, "INPUT_TOO_LARGE"):
            store.resolve_readable("roads", "vector")

    def test_shapefile_companions_count_toward_limit(self):
        make_vector(self.root / "points.shp")
        self.entries["points"] = {"path": "points.shp", "kind": "vector"}
        write_catalog(self.root, self.entries)
        settings = replace(self.settings, max_input_bytes=(self.root / "points.shp").stat().st_size + 1)
        with self.assertRaisesRegex(GeoError, "INPUT_TOO_LARGE"):
            DatasetStore(settings).resolve_readable("points", "vector")

    def test_catalog_rejects_unsafe_paths(self):
        for path in ["../escape.geojson", "a/../../escape.geojson", "C:\\escape.geojson", "C:escape.geojson", "/tmp/a.geojson", "\\\\server\\share\\a.geojson", "https://example.com/a.geojson", "file.geojson:secret", "a\\..\\b.geojson", "a\x00.geojson"]:
            with self.subTest(path=path):
                write_catalog(self.root, {"bad": {"path": path, "kind": "vector"}})
                with self.assertRaises(ConfigurationError):
                    DatasetStore(self.settings)

    def test_catalog_rejects_invalid_id_and_extra_fields(self):
        for entries in [{"../bad": {"path": "roads.geojson", "kind": "vector"}}, {"bad": {"path": "roads.geojson", "kind": "vector", "extra": True}}]:
            write_catalog(self.root, entries)
            with self.assertRaises(ConfigurationError):
                DatasetStore(self.settings)

    def test_duplicate_ids_rejected(self):
        (self.root / "datasets.json").write_text('{"datasets":{"x":{"path":"a.geojson","kind":"vector"},"x":{"path":"b.geojson","kind":"vector"}}}', encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            DatasetStore(self.settings)

    def test_symlink_or_junction_escape(self):
        outside = self.parent / "outside"
        outside.mkdir()
        (outside / "outside.geojson").write_text("{}", encoding="utf-8")
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
            self.addCleanup(link.unlink)
        except OSError:
            if os.name != "nt":
                self.skipTest("OS disallows symlink creation")
            env = dict(os.environ, GEO_TEST_LINK=str(link), GEO_TEST_TARGET=str(outside))
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction -Path $env:GEO_TEST_LINK -Target $env:GEO_TEST_TARGET | Out-Null"],
                env=env, capture_output=True, timeout=15,
            )
            if result.returncode != 0:
                self.skipTest("OS disallows both symlink and junction creation")
            # Remove only the junction itself, never recurse into its target.
            self.addCleanup(link.rmdir)
        self.entries["link"] = {"path": "link/outside.geojson", "kind": "vector"}
        write_catalog(self.root, self.entries)
        with self.assertRaisesRegex(GeoError, "PATH_NOT_ALLOWED"):
            DatasetStore(self.settings).resolve_readable("link", "vector")

    def test_unsupported_registered_format(self):
        (self.root / "bad.vrt").write_text("<VRTDataset/>", encoding="utf-8")
        write_catalog(self.root, {"bad": {"path": "bad.vrt", "kind": "raster"}})
        with self.assertRaisesRegex(GeoError, "UNSUPPORTED_FORMAT"):
            DatasetStore(self.settings).resolve_readable("bad", "raster")

    def test_environment_is_explicit(self):
        with self.assertRaises(ConfigurationError):
            Settings.from_environment({})
        with self.assertRaises(ConfigurationError):
            Settings.from_environment({"GEO_DATA_ROOT": "relative"})
        settings = Settings.from_environment({"GEO_DATA_ROOT": str(self.root)})
        self.assertEqual(settings.catalog_path, self.root / "datasets.json")
        self.assertEqual(settings.max_vector_features, 1000000)

    def test_invalid_limits_and_log_level(self):
        for key, value in [("GEO_MAX_INPUT_BYTES", "0"), ("GEO_MAX_VECTOR_FEATURES", "-1"), ("GEO_MAX_INPUT_BYTES", "abc"), ("GEO_LOG_LEVEL", "LOUD")]:
            with self.subTest(key=key), self.assertRaises(ConfigurationError):
                Settings.from_environment({"GEO_DATA_ROOT": str(self.root), key: value})

    def test_catalog_version_and_size(self):
        for data in ['{"version":2,"datasets":{}}', ' ' * (1024 * 1024 + 1)]:
            (self.root / "datasets.json").write_text(data, encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                DatasetStore(self.settings)

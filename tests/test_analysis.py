import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image
from pyproj import CRS, Transformer
from shapely.geometry import Point, Polygon, box

from analysis_fixtures import make_analysis_fixture, write_features
from geospatial_mcp.errors import GeoError
from geospatial_mcp.operations import AnalysisService
from geospatial_mcp.schemas import SourceRef
from geospatial_mcp.services.cartography import render_vector_map
from geospatial_mcp.services.vector import Row, VectorData, buffer_vector, clip_vector, load_vector, spatial_join, write_vector
from geospatial_mcp.settings import Settings
from geospatial_mcp.storage import DatasetStore
from geospatial_mcp.workspace import TaskStore, digest


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        make_analysis_fixture(self.data)
        self.settings = Settings(self.data, self.data / "datasets.json", workspace_root=self.root / "workspace")
        self.datasets = DatasetStore(self.settings)
        self.tasks = TaskStore(self.settings)
        self.service = AnalysisService(self.datasets, self.tasks)
        self.points = load_vector(self.data / "points.geojson")
        self.mask = load_vector(self.data / "mask.geojson")

    def test_buffer_distance_attributes_and_crs(self):
        data, warnings = buffer_vector(self.points, 10)
        self.assertEqual(data.crs.to_epsg(), 32650)
        self.assertEqual(data.rows[0].properties, {"id": 1, "value": 10.0})
        self.assertAlmostEqual(data.rows[0].geometry.area, math.pi * 100, delta=0.6)
        self.assertEqual(data.rows[0].geometry.bounds, (499990, 3399990, 500010, 3400010))
        self.assertTrue(warnings)

    def test_buffer_geographic_requires_explicit_projected_crs(self):
        data = VectorData(CRS.from_epsg(4326), {}, [Row(Point(117, 30), {})])
        with self.assertRaisesRegex(GeoError, "PROJECTED_CRS_REQUIRED"):
            buffer_vector(data, 10)
        result, warnings = buffer_vector(data, 10, analysis_crs="EPSG:32650")
        self.assertEqual(result.crs.to_epsg(), 32650)
        self.assertAlmostEqual(result.rows[0].geometry.centroid.x, 500000, places=4)
        self.assertTrue(any("REPROJECTED" in item for item in warnings))

    def test_buffer_feet_are_converted_to_meters(self):
        crs = CRS.from_epsg(2277)
        x, y = Transformer.from_crs(4326, crs, always_xy=True).transform(-97, 30)
        data = VectorData(crs, {}, [Row(Point(x, y), {})])
        result, _ = buffer_vector(data, 1)
        self.assertAlmostEqual(result.rows[0].geometry.bounds[2] - x, 1 / crs.axis_info[0].unit_conversion_factor, places=7)

    def test_buffer_rejects_wrong_area_and_invalid_distance(self):
        data = VectorData(CRS.from_epsg(4326), {}, [Row(Point(-74, 40), {})])
        with self.assertRaisesRegex(GeoError, "OUTSIDE_CRS_AREA"):
            buffer_vector(data, 10, analysis_crs="EPSG:32650")
        for value in [0, -1, math.nan, math.inf, 1000001]:
            with self.subTest(value=value), self.assertRaisesRegex(GeoError, "INVALID_DISTANCE"):
                buffer_vector(self.points, value)
        for definition in ["not a crs", "file:///tmp/crs", "+proj=utm +zone=50"]:
            with self.subTest(definition=definition), self.assertRaisesRegex(GeoError, "INVALID_CRS"):
                buffer_vector(self.points, 1, analysis_crs=definition)

    def test_clip_preserves_holes_attributes_and_empty_result(self):
        donut = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], holes=[[(2, 2), (8, 2), (8, 8), (2, 8)]])
        source = VectorData(CRS.from_epsg(3857), {"id": "int"}, [Row(donut, {"id": 7})])
        mask = VectorData(source.crs, {}, [Row(box(-1, -1, 11, 11), {})])
        clipped = clip_vector(source, mask)
        self.assertEqual(clipped.rows[0].geometry.area, 64)
        self.assertEqual(len(clipped.rows[0].geometry.interiors), 1)
        self.assertEqual(clipped.rows[0].properties["id"], 7)
        empty = clip_vector(source, VectorData(source.crs, {}, [Row(box(20, 20, 21, 21), {})]))
        output = self.root / "empty.gpkg"
        write_vector(empty, output)
        self.assertEqual(load_vector(output).rows, [])

    def test_clip_requires_polygon_and_matching_crs(self):
        with self.assertRaisesRegex(GeoError, "POLYGON_MASK_REQUIRED"):
            clip_vector(self.points, self.points)
        other = VectorData(CRS.from_epsg(4326), self.mask.fields, self.mask.rows)
        with self.assertRaisesRegex(GeoError, "CRS_MISMATCH"):
            clip_vector(self.points, other)

    def test_join_one_to_many_unmatched_and_prefixes(self):
        result = spatial_join(self.points, self.mask)
        self.assertEqual(len(result.rows), 4)
        self.assertEqual([r.properties["__right_row"] for r in result.rows], [0, 0, 1, None])
        self.assertEqual(result.rows[-1].properties["left_id"], 3)
        self.assertIsNone(result.rows[-1].properties["right_value"])
        self.assertEqual(len(spatial_join(self.points, self.mask, how="inner").rows), 3)
        with self.assertRaisesRegex(GeoError, "OUTPUT_FEATURE_LIMIT"):
            spatial_join(self.points, self.mask, max_features=3)

    def test_join_predicate_direction_and_empty_right(self):
        self.assertEqual(len(spatial_join(self.points, self.mask, predicate="within", how="inner").rows), 3)
        self.assertEqual(len(spatial_join(self.mask, self.points, predicate="contains", how="inner").rows), 3)
        empty = VectorData(self.points.crs, self.mask.fields, [])
        self.assertEqual(len(spatial_join(self.points, empty).rows), 3)
        self.assertEqual(len(spatial_join(self.points, empty, how="inner").rows), 0)
        with self.assertRaisesRegex(GeoError, "CRS_MISMATCH"):
            spatial_join(self.points, VectorData(CRS.from_epsg(4326), self.mask.fields, self.mask.rows))
        with self.assertRaisesRegex(GeoError, "INVALID_JOIN_MODE"):
            spatial_join(self.points, self.mask, predicate="nearest")

    def test_invalid_empty_and_3d_geometries_rejected(self):
        examples = [(Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)]), "INVALID_GEOMETRY"),
                    (None, "EMPTY_GEOMETRY"), (Point(1, 2, 3), "UNSUPPORTED_DIMENSION")]
        for i, (geometry, code) in enumerate(examples):
            path = self.root / f"invalid-{i}.geojson"
            write_features(path, [geometry])
            with self.subTest(code=code), self.assertRaisesRegex(GeoError, code):
                load_vector(path)

    def test_missing_crs_and_input_limit(self):
        path = self.root / "missing.gpkg"
        write_features(path, [Point(0, 0)], crs=None)
        with self.assertRaisesRegex(GeoError, "CRS_REQUIRED"):
            load_vector(path)
        with self.assertRaisesRegex(GeoError, "FEATURE_LIMIT_EXCEEDED"):
            load_vector(self.data / "points.geojson", max_features=2)

    def test_gpkg_preserves_fid_attribute_and_refuses_overwrite(self):
        data = VectorData(self.points.crs, {"fid": "str", "__geo_fid": "int", "value": "float"},
                          [Row(self.points.rows[0].geometry, {"fid": "external-id", "__geo_fid": 42, "value": 10.0})])
        path = self.root / "fields.gpkg"
        write_vector(data, path)
        self.assertEqual(load_vector(path).rows[0].properties, data.rows[0].properties)
        with self.assertRaisesRegex(GeoError, "OUTPUT_EXISTS"):
            write_vector(data, path)

    def test_output_unsupported_attributes_are_not_silently_lost(self):
        data = VectorData(self.points.crs, {"A": "str", "a": "str"}, [])
        with self.assertRaisesRegex(GeoError, "UNSUPPORTED_FIELD_NAME"):
            write_vector(data, self.root / "case.gpkg")
        data.fields = {"items": "List[str]"}
        with self.assertRaisesRegex(GeoError, "UNSUPPORTED_FIELD_TYPE"):
            write_vector(data, self.root / "list.gpkg")

    def test_map_png_size_values_and_hole(self):
        donut = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], holes=[[(2, 2), (8, 2), (8, 8), (2, 8)]])
        data = VectorData(CRS.from_epsg(3857), {"v": "float"}, [Row(donut, {"v": 3.0})])
        output = self.root / "map.png"
        result, warnings = render_vector_map(data, output, value_field="v", width=1000, height=700)
        self.assertEqual(result["value_min"], 3)
        with Image.open(output) as image:
            self.assertEqual(image.size, (1000, 700))
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.getpixel((440, 342)), (255, 255, 255))
            self.assertNotEqual(image.getpixel((440, 130)), (255, 255, 255))
        self.assertTrue(warnings)

    def test_map_rejects_empty_non_numeric_and_missing_fields(self):
        path = self.root / "bad.png"
        with self.assertRaisesRegex(GeoError, "EMPTY_DATASET"):
            render_vector_map(VectorData(self.points.crs, {}, []), path)
        with self.assertRaisesRegex(GeoError, "FIELD_NOT_FOUND"):
            render_vector_map(self.points, path, value_field="missing")
        self.points.rows[0].properties["value"] = None
        with self.assertRaisesRegex(GeoError, "NUMERIC_FIELD_REQUIRED"):
            render_vector_map(self.points, path, value_field="value")

    def test_service_chain_retains_inputs_and_task_provenance(self):
        before = {p.name: digest(p) for p in self.data.iterdir()}
        buffered = self.service.buffer(SourceRef(dataset_id="points"), 10)
        clipped = self.service.clip(SourceRef(artifact_id=buffered.artifacts[0].artifact_id), SourceRef(dataset_id="mask"))
        joined = self.service.join(SourceRef(artifact_id=clipped.artifacts[0].artifact_id), SourceRef(dataset_id="mask"))
        mapped = self.service.render(SourceRef(artifact_id=joined.artifacts[0].artifact_id), value_field="left_value", title="Buffer coverage")
        self.assertEqual([r.summary["feature_count"] for r in [buffered, clipped, joined, mapped]], [3, 2, 3, 3])
        self.assertEqual(before, {p.name: digest(p) for p in self.data.iterdir()})
        task = self.tasks.get(joined.task_id)
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(len(task.inputs), 2)
        self.assertEqual(task.inputs[0].sha256, clipped.artifacts[0].sha256)
        self.assertIsNotNone(task.finished_at)
        info, content = self.tasks.read(mapped.artifacts[0].artifact_id)
        self.assertTrue(content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(info.mime_type, "image/png")

    def test_failed_task_has_no_published_artifact(self):
        with self.assertRaisesRegex(GeoError, "POLYGON_MASK_REQUIRED.*task_id="):
            self.service.clip(SourceRef(dataset_id="points"), SourceRef(dataset_id="points"))
        directory = next((self.settings.workspace_root / "tasks").iterdir())
        task = self.tasks.get(directory.name)
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.error_code, "POLYGON_MASK_REQUIRED")
        self.assertEqual(task.artifacts, [])
        self.assertEqual(list((directory / "outputs").iterdir()), [])

    def test_size_limit_and_image_as_input_fail_cleanly(self):
        small = replace(self.settings, max_artifact_bytes=100)
        service = AnalysisService(self.datasets, TaskStore(small))
        with self.assertRaisesRegex(GeoError, "ARTIFACT_TOO_LARGE"):
            service.buffer(SourceRef(dataset_id="points"), 10)
        rendered = self.service.render(SourceRef(dataset_id="points"))
        with self.assertRaisesRegex(GeoError, "ARTIFACT_TYPE_MISMATCH"):
            self.service.buffer(SourceRef(artifact_id=rendered.artifacts[0].artifact_id), 10)

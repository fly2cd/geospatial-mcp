import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import fiona
from rasterio.transform import Affine, from_origin

from fixtures import make_raster, make_vector
from geospatial_mcp.errors import GeoError
from geospatial_mcp.services.data import inspect_raster, inspect_vector


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_vector_metadata_and_input_unchanged(self):
        path = self.root / "points.geojson"
        make_vector(path, null=True)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = inspect_vector(path)
        self.assertEqual(result.feature_count, 3)
        self.assertEqual(result.geometry_types, ["Point"])
        self.assertEqual(result.null_geometries, 1)
        self.assertEqual(result.crs, "EPSG:32650")
        self.assertEqual(result.bounds, (500000, 3400000, 500100, 3400100))
        self.assertEqual([field.name for field in result.fields], ["id", "name"])
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
        json.dumps(result.model_dump(), allow_nan=False)

    def test_empty_vector_missing_crs(self):
        path = self.root / "empty.shp"
        make_vector(path, crs=None, empty=True)
        result = inspect_vector(path)
        self.assertEqual(result.feature_count, 0)
        self.assertIsNone(result.bounds)
        self.assertIsNone(result.crs)
        self.assertEqual(result.crs_type, "undefined")
        self.assertTrue(any("CRS_MISSING" in warning for warning in result.warnings))
        json.dumps(result.model_dump(), allow_nan=False)

    def test_only_null_geometry(self):
        path = self.root / "null.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": None}]}), encoding="utf-8")
        result = inspect_vector(path)
        self.assertEqual(result.feature_count, 1)
        self.assertEqual(result.null_geometries, 1)
        self.assertIsNone(result.bounds)
        self.assertEqual(result.geometry_types, [])

    def test_empty_geometry_is_json_safe(self):
        path = self.root / "empty_geometry.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": []}}]}), encoding="utf-8")
        result = inspect_vector(path)
        self.assertIsNone(result.bounds)
        json.dumps(result.model_dump(), allow_nan=False)

    def test_geometry_collection_with_empty_member(self):
        path = self.root / "collection.geojson"
        geometry = {"type": "GeometryCollection", "geometries": [{"type": "Polygon", "coordinates": []}, {"type": "Point", "coordinates": [5, 7]}]}
        path.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": geometry}]}), encoding="utf-8")
        result = inspect_vector(path)
        self.assertEqual(result.bounds, (5, 7, 5, 7))
        self.assertEqual(result.geometry_types, ["GeometryCollection"])

    def test_polygon_hole_not_modified(self):
        path = self.root / "holes.geojson"
        geometry = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], [[2, 2], [2, 3], [3, 3], [3, 2], [2, 2]]]}
        path.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": geometry}]}), encoding="utf-8")
        before = path.read_bytes()
        result = inspect_vector(path)
        self.assertEqual(result.bounds, (0, 0, 10, 10))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result.geometry_types, ["Polygon"])

    def test_feature_limit(self):
        path = self.root / "points.geojson"
        make_vector(path)
        with self.assertRaisesRegex(GeoError, "FEATURE_LIMIT_EXCEEDED"):
            inspect_vector(path, max_features=1)

    def test_multilayer_requires_explicit_layer(self):
        path = self.root / "multi.gpkg"
        make_vector(path, layer="first")
        make_vector(path, layer="second", empty=True)
        with self.assertRaisesRegex(GeoError, "LAYER_REQUIRED"):
            inspect_vector(path)
        result = inspect_vector(path, layer="second")
        self.assertEqual(result.feature_count, 0)
        self.assertEqual(set(result.available_layers), {"first", "second"})
        with self.assertRaisesRegex(GeoError, "LAYER_NOT_FOUND"):
            inspect_vector(path, layer="unknown")

    def test_corrupt_vector_and_wrong_driver(self):
        for name, contents in [("bad.geojson", "not json"), ("disguised.geojson", '<OGRVRTDataSource/>')]:
            with self.subTest(name=name):
                path = self.root / name
                path.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(GeoError, "INVALID_VECTOR_DATA") as caught:
                    inspect_vector(path)
                self.assertNotIn(str(self.root), str(caught.exception))

    def test_raster_metadata(self):
        path = self.root / "dem.tif"
        make_raster(path)
        before = path.read_bytes()
        result = inspect_raster(path)
        self.assertEqual((result.width, result.height, result.band_count), (4, 3, 1))
        self.assertEqual(result.bounds, (500000, 3399940, 500040, 3400000))
        self.assertEqual(result.pixel_size, (10, 20))
        self.assertEqual(result.pixel_size_meters, (10, 20))
        self.assertEqual(result.bands[0].nodata, -9999)
        self.assertEqual(result.bands[0].description, "Elevation")
        self.assertEqual(path.read_bytes(), before)
        json.dumps(result.model_dump(), allow_nan=False)

    def test_non_metric_projected_crs(self):
        path = self.root / "feet.tif"
        make_raster(path, crs="EPSG:2263", transform=from_origin(1000000, 200000, 10, 20))
        result = inspect_raster(path)
        self.assertEqual(result.pixel_size, (10, 20))
        self.assertAlmostEqual(result.pixel_size_meters[0], 10 * 1200 / 3937)
        self.assertAlmostEqual(result.pixel_size_meters[1], 20 * 1200 / 3937)
        self.assertIn("foot", result.coordinate_units[0].lower())

    def test_rotated_pixel_size(self):
        path = self.root / "rotated.tif"
        make_raster(path, transform=Affine(3, -5, 500000, 4, 12, 3400000))
        result = inspect_raster(path)
        self.assertEqual(result.pixel_size, (5, 13))
        self.assertEqual(result.pixel_size_meters, (5, 13))

    def test_geographic_crs_no_guessed_meters(self):
        path = self.root / "degrees.tif"
        make_raster(path, crs="EPSG:4326", transform=from_origin(116, 40, .01, .01))
        result = inspect_raster(path)
        self.assertIsNone(result.pixel_size_meters)
        self.assertEqual(result.crs_type, "geographic")

    def test_raster_missing_crs(self):
        path = self.root / "no_crs.tif"
        make_raster(path, crs=None)
        result = inspect_raster(path)
        self.assertIsNone(result.crs)
        self.assertIsNone(result.pixel_size_meters)
        self.assertTrue(result.warnings)

    def test_nonfinite_nodata_is_standard_json(self):
        for value, expected in [(float("nan"), "NaN"), (float("inf"), "+Infinity"), (float("-inf"), "-Infinity")]:
            with self.subTest(value=expected):
                path = self.root / "nonfinite.tif"
                make_raster(path, nodata=value)
                result = inspect_raster(path)
                self.assertEqual(result.bands[0].nodata, expected)
                json.dumps(result.model_dump(), allow_nan=False)

    def test_corrupt_raster_and_vrt_disguised_as_tiff(self):
        for content in ["not a TIFF", '<VRTDataset rasterXSize="2" rasterYSize="2"/>']:
            path = self.root / "invalid.tif"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(GeoError, "INVALID_RASTER_DATA") as caught:
                inspect_raster(path)
            self.assertNotIn(str(self.root), str(caught.exception))

    def test_unsupported_extensions(self):
        with self.assertRaisesRegex(GeoError, "UNSUPPORTED_FORMAT"):
            inspect_vector(self.root / "unsafe.pkl")
        with self.assertRaisesRegex(GeoError, "UNSUPPORTED_FORMAT"):
            inspect_raster(self.root / "unsafe.vrt")

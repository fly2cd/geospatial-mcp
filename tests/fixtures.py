"""Small local fixtures; no benchmark configuration, downloads or model calls."""

import json
from pathlib import Path

import fiona
import numpy as np
import rasterio
from rasterio.transform import from_origin


def make_vector(path: Path, *, crs="EPSG:32650", empty=False, null=False, layer=None):
    driver = {".geojson": "GeoJSON", ".gpkg": "GPKG", ".shp": "ESRI Shapefile"}[path.suffix]
    options = {"layer": layer} if layer else {}
    with fiona.open(path, "w", driver=driver, schema={"geometry": "Point", "properties": {"id": "int", "name": "str"}}, crs=crs, **options) as dst:
        if not empty:
            dst.write({"geometry": {"type": "Point", "coordinates": (500000.0, 3400000.0)}, "properties": {"id": 1, "name": "A"}})
            dst.write({"geometry": {"type": "Point", "coordinates": (500100.0, 3400100.0)}, "properties": {"id": 2, "name": "B"}})
            if null:
                dst.write({"geometry": None, "properties": {"id": 3, "name": "missing"}})


def make_raster(path: Path, *, crs="EPSG:32650", nodata=-9999.0, transform=None):
    with rasterio.open(path, "w", driver="GTiff", width=4, height=3, count=1, dtype="float32", crs=crs, nodata=nodata, transform=transform or from_origin(500000, 3400000, 10, 20)) as dst:
        values = np.arange(12, dtype="float32").reshape(3, 4)
        if nodata is not None:
            values[0, 0] = nodata
        dst.write(values, 1)
        dst.set_band_description(1, "Elevation")


def make_fixture(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    make_vector(root / "roads.geojson", null=True)
    make_raster(root / "dem.tif")
    entries = {"roads": {"path": "roads.geojson", "kind": "vector"}, "dem": {"path": "dem.tif", "kind": "raster"}}
    write_catalog(root, entries)
    return entries


def write_catalog(root: Path, entries: dict):
    (root / "datasets.json").write_text(json.dumps({"version": 1, "datasets": entries}), encoding="utf-8")

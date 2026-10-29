import json
from pathlib import Path

import fiona
from shapely.geometry import Point, box, mapping


def write_features(path: Path, geometries, *, fields=None, properties=None, crs="EPSG:32650"):
    fields = fields or {"id": "int", "value": "float"}
    properties = properties or [{"id": i + 1, "value": float((i + 1) * 10)} for i in range(len(geometries))]
    with fiona.open(path, "w", driver="GPKG" if path.suffix == ".gpkg" else "GeoJSON",
                    crs=crs, schema={"geometry": "Unknown", "properties": fields}) as dst:
        for geometry, attrs in zip(geometries, properties):
            dst.write({"geometry": mapping(geometry) if geometry is not None else None, "properties": attrs})


def make_analysis_fixture(root: Path):
    root.mkdir(parents=True)
    x, y = 500000, 3400000
    write_features(root / "points.geojson", [Point(x, y), Point(x + 30, y), Point(x + 200, y)])
    write_features(root / "mask.geojson", [box(x - 20, y - 20, x + 60, y + 20), box(x + 20, y - 20, x + 70, y + 20)])
    (root / "datasets.json").write_text(json.dumps({"version": 1, "datasets": {
        "points": {"path": "points.geojson", "kind": "vector"},
        "mask": {"path": "mask.geojson", "kind": "vector"},
    }}), encoding="utf-8")

"""Create a tiny local demo catalog without downloading or overwriting datasets."""

import argparse
import json
from pathlib import Path

import fiona
import numpy as np
import rasterio
from rasterio.transform import from_origin


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ("roads.geojson", "dem.tif", "datasets.json"):
        if (root / name).exists():
            parser.error(f"Refusing to overwrite {root / name}")
    with fiona.open(root / "roads.geojson", "w", driver="GeoJSON", crs="EPSG:32650", schema={"geometry": "LineString", "properties": {"id": "int", "name": "str"}}) as dst:
        for i in range(2):
            dst.write({"geometry": {"type": "LineString", "coordinates": [(500000, 3400000 + i * 100), (500100, 3400000 + i * 100)]}, "properties": {"id": i + 1, "name": f"road_{i + 1}"}})
    with rasterio.open(root / "dem.tif", "w", driver="GTiff", width=4, height=3, count=1, dtype="float32", crs="EPSG:32650", transform=from_origin(500000, 3400000, 10, 20), nodata=-9999) as dst:
        dst.write(np.arange(12, dtype="float32").reshape(3, 4), 1)
    catalog = {"version": 1, "datasets": {"roads_demo": {"path": "roads.geojson", "kind": "vector"}, "dem_demo": {"path": "dem.tif", "kind": "raster"}}}
    (root / "datasets.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    print(f"Demo data created. Set GEO_DATA_ROOT={root}")


if __name__ == "__main__":
    main()

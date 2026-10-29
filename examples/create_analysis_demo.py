"""Generate deterministic stage B fixtures; never download data or overwrite a directory."""

import argparse
import json
from pathlib import Path

import fiona
from shapely.geometry import Point, box, mapping


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    if root.exists():
        parser.error("Choose a new output directory; existing directories are not overwritten.")
    root.mkdir(parents=True)
    x, y = 500000, 3400000
    collections = {
        "points": [Point(x, y), Point(x + 30, y), Point(x + 200, y)],
        "mask": [box(x - 20, y - 20, x + 60, y + 20), box(x + 20, y - 20, x + 70, y + 20)],
    }
    for name, geometries in collections.items():
        with fiona.open(root / f"{name}.geojson", "w", driver="GeoJSON", crs="EPSG:32650",
                        schema={"geometry": "Unknown", "properties": {"id": "int", "value": "float"}}) as dst:
            for i, geometry in enumerate(geometries):
                dst.write({"geometry": mapping(geometry), "properties": {"id": i + 1, "value": float((i + 1) * 10)}})
    (root / "datasets.json").write_text(json.dumps({"version": 1, "datasets": {name: {"path": f"{name}.geojson", "kind": "vector"} for name in collections}}, indent=2), encoding="utf-8")
    print(f"Created stage B fixture catalog: {root}")


if __name__ == "__main__":
    main()

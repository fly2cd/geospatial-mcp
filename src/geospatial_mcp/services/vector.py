"""Deterministic planar vector operations, without MCP or benchmark configuration."""

from dataclasses import dataclass
import math
import re
from pathlib import Path

import fiona
import numpy as np
from pyproj import CRS, Transformer
import shapely
from shapely.geometry import mapping, shape
from shapely.ops import transform
from shapely.strtree import STRtree

from ..errors import GeoError
from .data import inspect_vector, VECTOR_DRIVERS


@dataclass
class Row:
    geometry: object
    properties: dict


@dataclass
class VectorData:
    crs: CRS
    fields: dict[str, str]
    rows: list[Row]


def validate_geometry(geometry) -> None:
    if geometry is None or geometry.is_empty:
        raise GeoError("EMPTY_GEOMETRY", "Analysis does not accept null or empty geometries.")
    if geometry.has_z or shapely.has_m(geometry):
        raise GeoError("UNSUPPORTED_DIMENSION", "Stage B analysis accepts only 2D geometries.")
    coordinates = shapely.get_coordinates(geometry)
    if not np.isfinite(coordinates).all() or not geometry.is_valid:
        raise GeoError("INVALID_GEOMETRY", "Invalid or non-finite geometry; no implicit repair is performed.")


def load_vector(path: Path, *, layer: str | None = None, max_features=100_000) -> VectorData:
    metadata = inspect_vector(path, layer=layer, max_features=max_features)
    if metadata.crs is None:
        raise GeoError("CRS_REQUIRED", "Analysis requires an explicit source CRS.")
    with fiona.open(path, layer=metadata.layer, enabled_drivers=[VECTOR_DRIVERS[path.suffix.lower()]]) as src:
        rows = []
        for feature in src:
            if len(rows) >= max_features:
                raise GeoError("FEATURE_LIMIT_EXCEEDED", "Analysis input exceeds GEO_MAX_ANALYSIS_FEATURES.")
            geometry = shape(feature["geometry"]) if feature["geometry"] is not None else None
            validate_geometry(geometry)
            properties = dict(feature["properties"])
            for value in properties.values():
                if isinstance(value, float) and not math.isfinite(value):
                    raise GeoError("INVALID_ATTRIBUTE", "Non-finite numeric attributes are not supported.")
            rows.append(Row(geometry, properties))
        return VectorData(CRS.from_user_input(src.crs_wkt), dict(src.schema["properties"]), rows)


def write_vector(data: VectorData, output: Path, *, max_features=100_000) -> None:
    if output.exists():
        raise GeoError("OUTPUT_EXISTS", "Output must not already exist.")
    if len(data.rows) > max_features:
        raise GeoError("OUTPUT_FEATURE_LIMIT", "Result exceeds GEO_MAX_OUTPUT_FEATURES.")
    for kind in data.fields.values():
        if kind.split(":")[0] not in {"str", "int", "int32", "int64", "float", "bool", "date", "datetime"}:
            raise GeoError("UNSUPPORTED_FIELD_TYPE", "This attribute type cannot be preserved in the GPKG output.")
    # SQLite field names are case-insensitive. Never silently drop/collapse attributes.
    lowered = [name.casefold() for name in data.fields]
    if len(set(lowered)) != len(lowered) or any(not name or "\x00" in name for name in data.fields):
        raise GeoError("UNSUPPORTED_FIELD_NAME", "Output fields must be nonempty and case-insensitively unique.")
    fid, geom = "__geo_fid", "__geo_geom"
    while fid.casefold() in lowered:
        fid += "_"
    while geom.casefold() in lowered or geom == fid:
        geom += "_"
    with fiona.open(output, "w", driver="GPKG", layer="result", crs_wkt=data.crs.to_wkt(),
                    schema={"geometry": "Unknown", "properties": data.fields}, FID=fid, GEOMETRY_NAME=geom) as dst:
        for row in data.rows:
            validate_geometry(row.geometry)
            dst.write({"geometry": mapping(row.geometry), "properties": row.properties})


def require_same_crs(left: VectorData, right: VectorData) -> None:
    if not left.crs.equals(right.crs, ignore_axis_order=True):
        raise GeoError("CRS_MISMATCH", "Input CRSs differ; reproject explicitly before clip/join.")


def buffer_vector(data: VectorData, distance_meters: float, *, analysis_crs: str | None = None) -> tuple[VectorData, list[str]]:
    if not math.isfinite(distance_meters) or not 0 < distance_meters <= 1_000_000:
        raise GeoError("INVALID_DISTANCE", "Buffer distance must be finite, positive and at most 1,000,000 metres.")
    if analysis_crs is not None and not re.fullmatch(r"EPSG:[1-9][0-9]{2,5}", analysis_crs):
        raise GeoError("INVALID_CRS", "Explicit analysis_crs must be an EPSG identifier, not a path, URL or custom PROJ definition.")
    try:
        target = CRS.from_user_input(analysis_crs) if analysis_crs is not None else data.crs
    except Exception as exc:
        raise GeoError("INVALID_CRS", "analysis_crs is not a supported CRS definition.") from exc
    if not target.is_projected or len(target.axis_info) != 2:
        raise GeoError("PROJECTED_CRS_REQUIRED", "Buffering in metres requires an explicit 2D projected analysis CRS.")
    factors = [axis.unit_conversion_factor for axis in target.axis_info]
    if not all(math.isfinite(f) and f > 0 for f in factors) or not math.isclose(*factors, rel_tol=1e-12):
        raise GeoError("UNSUPPORTED_CRS_UNITS", "Analysis CRS axes must use the same known linear unit.")
    rows = data.rows
    warnings = ["PLANAR_DISTANCE: distance is measured on the projection plane, not a geodesic ground distance."]
    if not target.equals(data.crs, ignore_axis_order=True):
        converter = Transformer.from_crs(data.crs, target, always_xy=True, allow_ballpark=False, only_best=True)
        rows = [Row(transform(converter.transform, r.geometry), dict(r.properties)) for r in rows]
        warnings.append("REPROJECTED: coordinates were transformed to the explicit analysis CRS; output retains that CRS.")
    for row in rows:
        validate_geometry(row.geometry)
    area = target.area_of_use
    if area is not None and rows:
        to_wgs84 = Transformer.from_crs(target, "EPSG:4326", always_xy=True, allow_ballpark=False, only_best=True)
        for row in rows:
            geographic = transform(to_wgs84.transform, row.geometry)
            coordinates = shapely.get_coordinates(geographic)
            longitude, latitude = coordinates[:, 0], coordinates[:, 1]
            lon_ok = ((longitude >= area.west - 1e-7) & (longitude <= area.east + 1e-7)) if area.west <= area.east else ((longitude >= area.west - 1e-7) | (longitude <= area.east + 1e-7))
            if not (np.isfinite(coordinates).all() and lon_ok.all() and (latitude >= area.south - 1e-7).all() and (latitude <= area.north + 1e-7).all()):
                raise GeoError("OUTSIDE_CRS_AREA", "Input falls outside the declared area of use of the analysis CRS.")
    if area is None:
        warnings.append("CRS_AREA_UNKNOWN: this CRS has no declared area of use; suitability is the caller's responsibility.")
    result = [Row(row.geometry.buffer(distance_meters / factors[0], quad_segs=16), dict(row.properties)) for row in rows]
    return VectorData(target, dict(data.fields), result), warnings


def clip_vector(data: VectorData, mask: VectorData) -> VectorData:
    require_same_crs(data, mask)
    if any(row.geometry.geom_type not in {"Polygon", "MultiPolygon"} for row in mask.rows):
        raise GeoError("POLYGON_MASK_REQUIRED", "Clip masks must contain only polygons or multipolygons.")
    boundary = shapely.union_all([row.geometry for row in mask.rows])
    rows = []
    for row in data.rows:
        clipped = row.geometry.intersection(boundary)
        if not clipped.is_empty:
            rows.append(Row(clipped, dict(row.properties)))
    return VectorData(data.crs, dict(data.fields), rows)


def spatial_join(left: VectorData, right: VectorData, *, predicate="intersects", how="left", max_features=100_000) -> VectorData:
    require_same_crs(left, right)
    if predicate not in {"intersects", "within", "contains"} or how not in {"left", "inner"}:
        raise GeoError("INVALID_JOIN_MODE", "Supported joins: left/inner and intersects/within/contains.")
    fields = {"__left_row": "int64", "__right_row": "int64"}
    fields.update({"left_" + k: v for k, v in left.fields.items()})
    fields.update({"right_" + k: v for k, v in right.fields.items()})
    index = STRtree([row.geometry for row in right.rows])
    rows = []
    for i, row in enumerate(left.rows):
        matches = sorted(int(j) for j in index.query(row.geometry, predicate=predicate))
        if not matches and how == "left":
            matches = [None]
        if len(rows) + len(matches) > max_features:
            raise GeoError("OUTPUT_FEATURE_LIMIT", "Spatial join exceeds GEO_MAX_OUTPUT_FEATURES.")
        for j in matches:
            props = {"__left_row": i, "__right_row": j}
            props.update({"left_" + k: row.properties[k] for k in left.fields})
            props.update({"right_" + k: right.rows[j].properties[k] if j is not None else None for k in right.fields})
            rows.append(Row(row.geometry, props))
    return VectorData(left.crs, fields, rows)

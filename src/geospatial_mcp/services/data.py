"""Metadata extraction derived from toolbox/Data.py, with no benchmark imports.

Unlike the legacy tools, inspection does not estimate areas, read all raster
pixels, sample randomly, repair geometry, or assume projected units are metres.
"""

import math
from pathlib import Path

import fiona
import rasterio
from pyproj import CRS

from ..errors import GeoError
from ..schemas import FieldInfo, RasterBand, RasterInfo, VectorInfo

VECTOR_DRIVERS = {".geojson": "GeoJSON", ".json": "GeoJSON", ".gpkg": "GPKG", ".shp": "ESRI Shapefile"}


def _crs_info(value):
    if not value:
        return None, "undefined", [], ["CRS_MISSING: coordinates are reported without guessing a CRS."]
    crs = CRS.from_user_input(value)
    kind = "geographic" if crs.is_geographic else "projected" if crs.is_projected else "other"
    return crs.to_string(), kind, [axis.unit_name for axis in crs.axis_info[:2]], []



def _geometry_bounds(geometry):
    """Handle empty members of a GeometryCollection without losing valid bounds."""
    if geometry["type"] == "GeometryCollection":
        parts = [_geometry_bounds(child) for child in geometry.get("geometries", [])]
        parts = [bounds for bounds in parts if bounds is not None and all(math.isfinite(v) for v in bounds)]
        if not parts:
            return None
        return (min(b[0] for b in parts), min(b[1] for b in parts), max(b[2] for b in parts), max(b[3] for b in parts))
    try:
        return fiona.bounds(geometry)
    except (IndexError, ValueError):
        # Fiona raises for empty coordinate sequences; never invent an extent.
        return None


def inspect_vector(input_path: Path, *, layer: str | None = None, max_features: int = 1_000_000) -> VectorInfo:
    """Inspect a supported vector layer, streaming features; bounds use source CRS units."""
    driver = VECTOR_DRIVERS.get(input_path.suffix.lower())
    if driver is None:
        raise GeoError("UNSUPPORTED_FORMAT", "Supported vector formats: GeoJSON, GeoPackage, Shapefile.")
    if max_features <= 0:
        raise ValueError("max_features must be positive")
    try:
        # Fiona 1.10 listlayers does not enforce enabled_drivers. Validate the
        # actual driver first; only then enumerate an already validated GPKG.
        with fiona.open(input_path, enabled_drivers=[driver]) as probe:
            if probe.driver != driver:
                raise GeoError("INVALID_VECTOR_DATA", "Vector driver does not match the allowed format.")
            layers = fiona.listlayers(input_path) if driver == "GPKG" else [probe.name]
        if not layers:
            raise GeoError("EMPTY_CONTAINER", "Vector dataset contains no layers.")
        if layer is None and len(layers) > 1:
            raise GeoError("LAYER_REQUIRED", "Dataset has multiple layers; supply an explicit layer name.")
        selected = layer if layer is not None else layers[0]
        if selected not in layers:
            raise GeoError("LAYER_NOT_FOUND", "Requested layer does not exist in this dataset.")
        with fiona.open(input_path, layer=selected, enabled_drivers=[driver]) as src:
            crs, crs_type, units, warnings = _crs_info(src.crs_wkt or src.crs)
            fields = [FieldInfo(name=name, data_type=str(dtype)) for name, dtype in src.schema["properties"].items()]
            count, nulls = 0, 0
            types = set()
            extent = None
            for feature in src:
                count += 1
                if count > max_features:
                    raise GeoError("FEATURE_LIMIT_EXCEEDED", "Layer exceeds GEO_MAX_VECTOR_FEATURES.")
                geometry = feature["geometry"]
                if geometry is None:
                    nulls += 1
                    continue
                types.add(geometry["type"])
                candidate = _geometry_bounds(geometry)
                if candidate is None or not all(math.isfinite(v) for v in candidate):
                    continue
                if extent is None:
                    extent = list(candidate)
                else:
                    extent = [min(extent[0], candidate[0]), min(extent[1], candidate[1]), max(extent[2], candidate[2]), max(extent[3], candidate[3])]
            if count == 0:
                warnings.append("EMPTY_DATASET: layer contains no features.")
            if nulls:
                warnings.append("NULL_GEOMETRIES: some features have no geometry.")
            if count and extent is None:
                warnings.append("NO_FINITE_BOUNDS: no non-empty geometry with finite bounds was found.")
            return VectorInfo(
                driver=src.driver, layer=selected, available_layers=layers, feature_count=count,
                geometry_types=sorted(types), crs=crs, crs_type=crs_type, coordinate_units=units,
                bounds=extent, fields=fields, null_geometries=nulls, warnings=warnings,
            )
    except GeoError:
        raise
    except Exception as exc:
        raise GeoError("INVALID_VECTOR_DATA", "Unable to inspect vector data; check format and geometry content.") from exc


def _json_nodata(value):
    if value is None:
        return None
    value = float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Infinity" if value > 0 else "-Infinity"
    return value


def inspect_raster(input_path: Path) -> RasterInfo:
    """Read GeoTIFF metadata only: no full-band scans or implicit metre conversion."""
    if input_path.suffix.lower() not in {".tif", ".tiff"}:
        raise GeoError("UNSUPPORTED_FORMAT", "Supported raster format: GeoTIFF.")
    try:
        # Do not load sidecars/overviews or allow GDAL virtual files/network drivers.
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
            with rasterio.open(input_path, driver="GTiff") as src:
                crs, crs_type, units, warnings = _crs_info(src.crs)
                transform = src.transform
                meters = None
                if src.crs and crs_type == "projected":
                    axes = CRS.from_user_input(src.crs).axis_info
                    if len(axes) >= 2:
                        xfactor, yfactor = axes[0].unit_conversion_factor, axes[1].unit_conversion_factor
                        if all(math.isfinite(f) and f > 0 for f in (xfactor, yfactor)):
                            meters = (
                                math.hypot(transform.a * xfactor, transform.d * yfactor),
                                math.hypot(transform.b * xfactor, transform.e * yfactor),
                            )
                elif crs_type == "geographic":
                    warnings.append("ANGULAR_UNITS: pixel_size_meters is null; no approximate degree-to-metre conversion.")
                if src.count > 1024:
                    raise GeoError("BAND_LIMIT_EXCEEDED", "At most 1024 raster bands can be inspected.")
                bands = [
                    RasterBand(band=i + 1, dtype=src.dtypes[i], nodata=_json_nodata(src.nodatavals[i]), description=src.descriptions[i], unit=src.units[i])
                    for i in range(src.count)
                ]
                return RasterInfo(
                    driver=src.driver, width=src.width, height=src.height, band_count=src.count,
                    crs=crs, crs_type=crs_type, coordinate_units=units, bounds=tuple(src.bounds),
                    transform=tuple(transform)[:6], pixel_size=tuple(src.res), pixel_size_meters=meters,
                    bands=bands, warnings=warnings,
                )
    except GeoError:
        raise
    except Exception as exc:
        raise GeoError("INVALID_RASTER_DATA", "Unable to inspect raster data; check that it is a valid GeoTIFF.") from exc

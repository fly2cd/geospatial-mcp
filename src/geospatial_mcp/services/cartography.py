"""Small deterministic PNG vector maps. No pyplot, external fonts, URLs or basemaps."""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..errors import GeoError
from .vector import VectorData


PALE = (226, 241, 248)
DARK = (13, 73, 116)
BACKGROUND = (247, 250, 252)


def _color(value: float, low: float, high: float):
    fraction = 0.5 if low == high else (value - low) / (high - low)
    return tuple(round(a + fraction * (b - a)) for a, b in zip(PALE, DARK))


def render_vector_map(data: VectorData, output: Path, *, value_field: str | None = None,
                      title="Vector map", width=1000, height=700) -> tuple[dict, list[str]]:
    if not data.rows:
        raise GeoError("EMPTY_DATASET", "A map requires at least one non-empty feature.")
    if not (480 <= width <= 2000 and 360 <= height <= 2000):
        raise GeoError("INVALID_IMAGE_SIZE", "Map dimensions exceed the supported range.")
    if len(title) > 120 or any(not 32 <= ord(c) <= 126 for c in title):
        raise GeoError("INVALID_MAP_TITLE", "Stage B's bundled font supports printable ASCII map titles (max 120 characters).")
    if output.exists():
        raise GeoError("OUTPUT_EXISTS", "Map output must not already exist.")
    values = None
    if value_field is not None:
        if value_field not in data.fields:
            raise GeoError("FIELD_NOT_FOUND", "The requested thematic field does not exist.")
        values = [row.properties[value_field] for row in data.rows]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise GeoError("NUMERIC_FIELD_REQUIRED", "Thematic values must all be finite, non-null numbers.")
    bounds = [min(r.geometry.bounds[0] for r in data.rows), min(r.geometry.bounds[1] for r in data.rows),
              max(r.geometry.bounds[2] for r in data.rows), max(r.geometry.bounds[3] for r in data.rows)]
    xmin, ymin, xmax, ymax = bounds
    xspan, yspan = xmax - xmin, ymax - ymin
    span = max(xspan, yspan, 1.0)
    xspan, yspan = max(xspan, span * 0.05), max(yspan, span * 0.05)
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    plot = (55, 75, width - 175, height - 90)
    scale = min((plot[2] - plot[0]) / xspan, (plot[3] - plot[1]) / yspan) * 0.9
    px, py = (plot[0] + plot[2]) / 2, (plot[1] + plot[3]) / 2

    def xy(point):
        return (round(px + (point[0] - cx) * scale), round(py - (point[1] - cy) * scale))

    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=16)
    heading = ImageFont.load_default(size=23)
    # Fit long titles rather than drawing outside the canvas.
    while draw.textbbox((0, 0), title, font=heading)[2] > width - 50 and len(title) > 3:
        title = title[:-4] + "..."
    draw.text((25, 23), title, fill=(24, 42, 60), font=heading)
    draw.rectangle(plot, fill="white", outline=(193, 205, 216))

    def paint(geometry, color):
        kind = geometry.geom_type
        if kind == "Polygon":
            mask = Image.new("L", image.size, 0)
            pen = ImageDraw.Draw(mask)
            exterior = [xy(p) for p in geometry.exterior.coords]
            pen.polygon(exterior, fill=255)
            for ring in geometry.interiors:
                pen.polygon([xy(p) for p in ring.coords], fill=0)
            image.paste(color, mask=mask)
            draw.line(exterior, fill=(27, 62, 87), width=1)
            for ring in geometry.interiors:
                draw.line([xy(p) for p in ring.coords], fill=(27, 62, 87), width=1)
        elif kind in {"LineString", "LinearRing"}:
            draw.line([xy(p) for p in geometry.coords], fill=color, width=3)
        elif kind == "Point":
            x, y = xy(geometry.coords[0])
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=color, outline=(27, 62, 87))
        elif hasattr(geometry, "geoms"):
            for part in geometry.geoms:
                paint(part, color)
        else:
            raise GeoError("UNSUPPORTED_GEOMETRY", "This geometry cannot be rendered.")

    low, high = (min(values), max(values)) if values is not None else (None, None)
    for i, row in enumerate(data.rows):
        paint(row.geometry, _color(values[i], low, high) if values is not None else (39, 127, 168))
    legend_x = width - 150
    draw.text((legend_x, 82), "Legend", fill=(24, 42, 60), font=font)
    if values is None:
        draw.rectangle((legend_x, 112, legend_x + 18, 130), fill=(39, 127, 168))
        draw.text((legend_x + 24, 113), "Features", fill=(24, 42, 60), font=font)
    else:
        label = value_field.encode("ascii", "backslashreplace").decode("ascii")
        draw.text((legend_x, 110), label[:17], fill=(24, 42, 60), font=font)
        for offset in range(160):
            draw.line((legend_x, 148 + offset, legend_x + 22, 148 + offset), fill=_color(high - (high - low) * offset / 159, low, high))
        draw.text((legend_x + 30, 144), f"{high:.5g}", fill=DARK, font=font)
        draw.text((legend_x + 30, 294), f"{low:.5g}", fill=DARK, font=font)
    crs = data.crs.to_string()
    crs_label = crs.encode("ascii", "backslashreplace").decode("ascii")[:65]
    draw.text((25, height - 65), f"CRS: {crs_label}", fill=(53, 71, 87), font=font)
    draw.text((25, height - 42), "Bounds: " + ", ".join(f"{v:.6g}" for v in bounds), fill=(53, 71, 87), font=font)
    draw.text((legend_x, height - 100), f"{len(data.rows)} features", fill=(53, 71, 87), font=font)
    image.save(output, format="PNG")
    warnings = ["CARTOGRAPHY_SCOPE: native-coordinate map without a basemap, geodesic scale bar or geographic north arrow."]
    if value_field is not None and not value_field.isascii():
        warnings.append("ASCII_LEGEND: non-ASCII field names are escaped in the bundled-font legend.")
    return {"width": width, "height": height, "feature_count": len(data.rows), "crs": crs,
            "bounds": bounds, "value_field": value_field, "value_min": low, "value_max": high}, warnings

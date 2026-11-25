"""Summary of linear geometry: counters, layers, bbox and length/orientation statistics.

Never returns the list of lines itself - only aggregates.
"""

import math
from collections import Counter

import ezdxf

SKILL = {
    "name": "extract_line_stats",
    "description": (
        "Counts entities by type and layer, computes the bbox of the drawing and "
        "statistics of straight segments (LINE + straight LWPOLYLINE/POLYLINE parts): "
        "count, total/min/max/median length, horizontal/vertical/other orientation. "
        "No geometry is returned. Call first to understand an exported fragment."
    ),
    "params": {
        "layers": [],
        "min_length": 0.0,
        "angle_tol_deg": 1.0,
        "top_layers": 30,
    },
}


def _segments(msp, layers):
    """Yield (layer, x1, y1, x2, y2) for LINE entities and straight polyline segments."""
    for entity in msp:
        kind = entity.dxftype()
        layer = entity.dxf.get("layer", "0")
        if layers and layer not in layers:
            continue
        if kind == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            yield layer, s.x, s.y, e.x, e.y
        elif kind == "LWPOLYLINE":
            points = list(entity.get_points("xyb"))
            if entity.closed and len(points) > 1:
                points.append(points[0])
            for (x1, y1, bulge), (x2, y2, _) in zip(points, points[1:]):
                if not bulge:
                    yield layer, x1, y1, x2, y2
        elif kind == "POLYLINE" and entity.is_2d_polyline:
            vertices = list(entity.vertices)
            pts = [(v.dxf.location.x, v.dxf.location.y, v.dxf.get("bulge", 0)) for v in vertices]
            if entity.is_closed and len(pts) > 1:
                pts.append(pts[0])
            for (x1, y1, bulge), (x2, y2, _) in zip(pts, pts[1:]):
                if not bulge:
                    yield layer, x1, y1, x2, y2


def run(dxf_path: str, params: dict) -> dict:
    layers_filter = set(params.get("layers") or [])
    min_length = float(params.get("min_length", 0.0))
    angle_tol = float(params.get("angle_tol_deg", 1.0))
    top_layers = int(params.get("top_layers", 30))

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    entity_types = Counter()
    entity_layers = Counter()
    for entity in msp:
        entity_types[entity.dxftype()] += 1
        entity_layers[entity.dxf.get("layer", "0")] += 1

    lengths = []
    seg_layers = Counter()
    orientation = Counter()
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    skipped_short = 0
    for layer, x1, y1, x2, y2 in _segments(msp, layers_filter):
        length = math.hypot(x2 - x1, y2 - y1)
        if length < min_length or length == 0:
            skipped_short += 1
            continue
        lengths.append(length)
        seg_layers[layer] += 1
        min_x, max_x = min(min_x, x1, x2), max(max_x, x1, x2)
        min_y, max_y = min(min_y, y1, y2), max(max_y, y1, y2)
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
        if angle <= angle_tol or angle >= 180.0 - angle_tol:
            orientation["horizontal"] += 1
        elif abs(angle - 90.0) <= angle_tol:
            orientation["vertical"] += 1
        else:
            orientation["other"] += 1

    warnings = []
    lengths.sort()
    length_stats = None
    if lengths:
        n = len(lengths)
        median = lengths[n // 2] if n % 2 else (lengths[n // 2 - 1] + lengths[n // 2]) / 2
        length_stats = {
            "min": round(lengths[0], 3),
            "max": round(lengths[-1], 3),
            "mean": round(sum(lengths) / n, 3),
            "median": round(median, 3),
            "total": round(sum(lengths), 3),
        }
    else:
        warnings.append("No straight segments found (check the layers filter / export).")

    bbox = None
    if lengths:
        bbox = {"min_x": round(min_x, 3), "min_y": round(min_y, 3), "max_x": round(max_x, 3), "max_y": round(max_y, 3)}

    return {
        "ok": True,
        "skill": SKILL["name"],
        "stats": {
            "entities_total": sum(entity_types.values()),
            "segments_total": len(lengths) + skipped_short,
            "segments_used": len(lengths),
            "segments_skipped_short": skipped_short,
            "layers_total": len(entity_layers),
        },
        "result": {
            "bbox": bbox,
            "entity_types": dict(entity_types.most_common()),
            "layers": [{"layer": name, "entities": count, "segments": seg_layers.get(name, 0)} for name, count in entity_layers.most_common(top_layers)],
            "length": length_stats,
            "orientation": dict(orientation),
        },
        "warnings": warnings,
    }

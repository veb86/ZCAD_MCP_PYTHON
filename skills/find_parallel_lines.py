"""Clusters straight segments by direction and returns compact groups.

Returns aggregated groups (direction, count, total length) and at most
``max_segments`` candidate segments (the longest ones of the largest groups),
ready for ``zcad_draw_lines``.
"""

import math

import ezdxf

SKILL = {
    "name": "find_parallel_lines",
    "description": (
        "Groups straight segments (LINE + straight polyline parts) by direction "
        "(angle modulo 180 deg, tolerance angle_tol_deg). Returns groups "
        "{angle_deg, count, total_length, max_length} and at most max_segments "
        "candidate segments {x1,y1,x2,y2,length,angle_deg,group,layer} of the "
        "largest groups - suitable for zcad_draw_lines."
    ),
    "params": {
        "angle_tol_deg": 1.0,
        "min_length": 0.0,
        "layers": [],
        "max_groups": 20,
        "precision": 3,
    },
}


def _segments(msp, layers):
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
            pts = [(v.dxf.location.x, v.dxf.location.y, v.dxf.get("bulge", 0)) for v in entity.vertices]
            if entity.is_closed and len(pts) > 1:
                pts.append(pts[0])
            for (x1, y1, bulge), (x2, y2, _) in zip(pts, pts[1:]):
                if not bulge:
                    yield layer, x1, y1, x2, y2


def _cluster_angles(items, tol):
    """Single-linkage clustering of angles in [0, 180) with wrap-around."""
    if not items:
        return []
    items.sort(key=lambda it: it["angle"])
    clusters = [[items[0]]]
    for item in items[1:]:
        if item["angle"] - clusters[-1][-1]["angle"] <= tol:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    # merge the last cluster into the first one across 180 -> 0
    if len(clusters) > 1 and (clusters[0][0]["angle"] + 180.0 - clusters[-1][-1]["angle"]) <= tol:
        last = clusters.pop()
        for item in last:
            item["angle"] -= 180.0
        clusters[0] = last + clusters[0]
    return clusters


def run(dxf_path: str, params: dict) -> dict:
    tol = float(params.get("angle_tol_deg", 1.0))
    min_length = float(params.get("min_length", 0.0))
    layers = set(params.get("layers") or [])
    max_groups = int(params.get("max_groups", 20))
    max_segments = int(params.get("max_segments", 200))
    digits = int(params.get("precision", 3))

    doc = ezdxf.readfile(dxf_path)
    items = []
    lines_total = 0
    for layer, x1, y1, x2, y2 in _segments(doc.modelspace(), layers):
        lines_total += 1
        length = math.hypot(x2 - x1, y2 - y1)
        if length <= 0 or length < min_length:
            continue
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
        items.append({"layer": layer, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "length": length, "angle": angle})

    clusters = _cluster_angles(items, tol)
    clusters.sort(key=lambda c: sum(i["length"] for i in c), reverse=True)

    groups = []
    for index, cluster in enumerate(clusters[:max_groups]):
        total = sum(i["length"] for i in cluster)
        mean_angle = sum(i["angle"] * i["length"] for i in cluster) / total % 180.0
        groups.append(
            {
                "group": index,
                "angle_deg": round(mean_angle, 3),
                "count": len(cluster),
                "total_length": round(total, digits),
                "max_length": round(max(i["length"] for i in cluster), digits),
            }
        )

    # Candidates: longest segments, distributed round-robin over the groups.
    per_group = [sorted(c, key=lambda i: i["length"], reverse=True) for c in clusters[:max_groups]]
    candidates = []
    depth = 0
    while len(candidates) < max_segments and any(depth < len(g) for g in per_group):
        for gi, group in enumerate(per_group):
            if depth < len(group) and len(candidates) < max_segments:
                it = group[depth]
                candidates.append(
                    {
                        "x1": round(it["x1"], digits),
                        "y1": round(it["y1"], digits),
                        "x2": round(it["x2"], digits),
                        "y2": round(it["y2"], digits),
                        "length": round(it["length"], digits),
                        "angle_deg": round(it["angle"] % 180.0, 3),
                        "group": gi,
                        "layer": it["layer"],
                    }
                )
        depth += 1

    warnings = []
    if len(clusters) > max_groups:
        warnings.append(f"{len(clusters) - max_groups} smaller direction groups omitted (max_groups={max_groups})")
    if len(items) > len(candidates):
        warnings.append(f"Returned {len(candidates)} of {len(items)} segments as candidates (max_segments={max_segments})")

    return {
        "ok": True,
        "skill": SKILL["name"],
        "stats": {
            "lines_total": lines_total,
            "lines_used": len(items),
            "groups": len(clusters),
            "candidates": len(candidates),
        },
        "result": {"groups": groups, "candidates": candidates},
        "warnings": warnings,
    }

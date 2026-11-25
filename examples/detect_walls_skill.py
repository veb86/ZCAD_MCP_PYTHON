"""Example of a skill an AI client creates on the fly with ``create_skill``.

Finds wall candidates: pairs of parallel straight segments whose distance lies in
[min_thickness, max_thickness] and whose projections overlap by at least
``min_overlap``. Each wall is returned as its centre line (a 2-point polyline,
ready for ``zcad_draw_polylines``) plus its thickness - never the raw lines.

Usage through MCP::

    create_skill(name="detect_walls", source_code=<this file>)
    run_skill(name="detect_walls", params_json='{"min_thickness": 100, "max_thickness": 400}')
    zcad_draw_polylines(polylines_json=<result.walls>, layer="IM_WALLS")
"""

import math

import ezdxf

SKILL = {
    "name": "detect_walls",
    "description": (
        "Wall candidates from pairs of parallel segments at wall-thickness distance. "
        "Returns walls [{points, closed, thickness, length}] (centre lines) limited by max_segments."
    ),
    "params": {
        "min_thickness": 50.0,
        "max_thickness": 600.0,
        "min_overlap": 200.0,
        "angle_tol_deg": 1.0,
        "layers": [],
    },
}


def _segments(msp, layers):
    for entity in msp:
        layer = entity.dxf.get("layer", "0")
        if layers and layer not in layers:
            continue
        kind = entity.dxftype()
        if kind == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            yield (s.x, s.y, e.x, e.y)
        elif kind == "LWPOLYLINE":
            pts = list(entity.get_points("xyb"))
            if entity.closed and len(pts) > 1:
                pts.append(pts[0])
            for (x1, y1, b), (x2, y2, _) in zip(pts, pts[1:]):
                if not b:
                    yield (x1, y1, x2, y2)


def run(dxf_path: str, params: dict) -> dict:
    t_min = float(params.get("min_thickness", 50.0))
    t_max = float(params.get("max_thickness", 600.0))
    min_overlap = float(params.get("min_overlap", 200.0))
    tol = math.radians(float(params.get("angle_tol_deg", 1.0)))
    layers = set(params.get("layers") or [])
    max_segments = int(params.get("max_segments", 200))

    doc = ezdxf.readfile(dxf_path)
    segs = []
    for x1, y1, x2, y2 in _segments(doc.modelspace(), layers):
        length = math.hypot(x2 - x1, y2 - y1)
        if length >= min_overlap:
            segs.append((x1, y1, x2, y2, length, math.atan2(y2 - y1, x2 - x1) % math.pi))
    segs.sort(key=lambda s: s[5])

    walls = []
    pairs_checked = 0
    for i, a in enumerate(segs):
        ax1, ay1, ax2, ay2, alen, aang = a
        ux, uy = (ax2 - ax1) / alen, (ay2 - ay1) / alen  # direction of a
        for b in segs[i + 1 :]:
            if b[5] - aang > tol:
                break  # sorted by angle: no more parallel candidates
            pairs_checked += 1
            bx1, by1, bx2, by2 = b[0], b[1], b[2], b[3]
            dist = abs((bx1 - ax1) * -uy + (by1 - ay1) * ux)
            if not t_min <= dist <= t_max:
                continue
            # overlap of projections on a's direction
            p1 = (bx1 - ax1) * ux + (by1 - ay1) * uy
            p2 = (bx2 - ax1) * ux + (by2 - ay1) * uy
            lo, hi = max(0.0, min(p1, p2)), min(alen, max(p1, p2))
            if hi - lo < min_overlap:
                continue
            side = (bx1 - ax1) * -uy + (by1 - ay1) * ux
            nx, ny = -uy * side / 2, ux * side / 2  # half way towards b
            start = (ax1 + ux * lo + nx, ay1 + uy * lo + ny)
            end = (ax1 + ux * hi + nx, ay1 + uy * hi + ny)
            walls.append(
                {
                    "points": [[round(start[0], 3), round(start[1], 3)], [round(end[0], 3), round(end[1], 3)]],
                    "closed": False,
                    "thickness": round(dist, 3),
                    "length": round(hi - lo, 3),
                }
            )
    walls.sort(key=lambda w: w["length"], reverse=True)
    warnings = []
    if len(walls) > max_segments:
        warnings.append(f"Returned {max_segments} of {len(walls)} walls (max_segments)")
    return {
        "ok": True,
        "skill": SKILL["name"],
        "stats": {"segments_used": len(segs), "pairs_checked": pairs_checked, "walls_found": len(walls)},
        "result": {"walls": walls[:max_segments]},
        "warnings": warnings,
    }

"""Skill contract and a minimal working example (copy it to ``skills/<name>.py``).

The MCP tool ``get_skill_template`` returns this file, so an AI client can read the
contract before generating a new skill with ``create_skill``.

Contract
--------
1. File ``skills/<name>.py``; ``name`` matches ``^[a-zA-Z_][a-zA-Z0-9_]*$`` and
   ``SKILL["name"]`` (if present) equals the file name.
2. ``SKILL`` - a *literal* dict: ``name``, ``description``, ``params`` (default
   values; their types become the JSON schema shown by ``list_skills``).
   Optionally ``params_schema`` - an explicit JSON schema.
3. ``run(dxf_path: str, params: dict) -> dict`` - a pure function of the drawing:
   read the DXF from ``dxf_path`` (``ezdxf.readfile``), analyse it, return a
   JSON-serializable dict. ``params`` always contains ``max_segments``.
4. Return ONLY compact data - counters, aggregates, groups and at most
   ``params["max_segments"]`` selected candidate segments. Never dump every line.
   Recommended shape::

       {"ok": true, "skill": "<name>",
        "stats": {...counters...},
        "result": {...groups / candidates...},
        "warnings": []}

   The runner cuts every list longer than ``max_segments`` (adds a warning) and
   rejects answers larger than ``ZCAD_MCP_MAX_RESULT_BYTES``.
5. Segments for drawing in ZCAD use ``{"x1", "y1", "x2", "y2"}`` (accepted by
   ``zcad_draw_lines``); polylines use ``{"points": [[x, y], ...], "closed": bool}``
   (accepted by ``zcad_draw_polylines``).
6. Forbidden (checked statically and at run time; the process is not a full
   sandbox): network, ``subprocess``/``os.system``/``os.exec*``, ``ctypes``,
   ``eval``/``exec``/``__import__``, ``importlib``, relative imports, writing
   outside ``workspace/``, reading files other than the DXF, ``workspace/`` and
   ``skills/``. Each run is a separate process killed after
   ``ZCAD_MCP_SKILL_TIMEOUT_SEC``. ``print()`` goes to the server log (stderr).
7. Skills cannot import each other: keep helpers inside the skill file.
   Available libraries: the standard library (minus the forbidden modules),
   ``ezdxf`` and ``numpy`` (if installed).
"""

import math

import ezdxf

SKILL = {
    "name": "skill_template",
    "description": "Example: counts LINE entities and their total length per layer.",
    "params": {
        "min_length": 0.0,
        "top_layers": 20,
    },
}


def run(dxf_path: str, params: dict) -> dict:
    min_length = float(params.get("min_length", 0.0))
    top_layers = int(params.get("top_layers", 20))

    doc = ezdxf.readfile(dxf_path)
    per_layer: dict = {}
    lines_total = 0
    for line in doc.modelspace().query("LINE"):
        lines_total += 1
        start, end = line.dxf.start, line.dxf.end
        length = math.hypot(end.x - start.x, end.y - start.y)
        if length < min_length:
            continue
        layer = per_layer.setdefault(line.dxf.layer, {"layer": line.dxf.layer, "count": 0, "length": 0.0})
        layer["count"] += 1
        layer["length"] += length

    layers = sorted(per_layer.values(), key=lambda item: item["length"], reverse=True)
    for item in layers:
        item["length"] = round(item["length"], 3)
    return {
        "ok": True,
        "skill": SKILL["name"],
        "stats": {"lines_total": lines_total, "lines_used": sum(i["count"] for i in layers), "layers": len(layers)},
        "result": {"layers": layers[:top_layers]},
        "warnings": [],
    }

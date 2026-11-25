"""Workspace helpers: the "current DXF" pointer and path resolution.

The current DXF is the file that ``run_skill`` analyses when no explicit
``dxf_path`` is given. ``zcad_export_selection`` and ``set_current_dxf`` update
it. The pointer is persisted in ``workspace/.current_dxf.json`` so it survives
server restarts; without it the default ``workspace/selection.dxf`` is used.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from pathlib import Path
from typing import Any

from config import Settings

log = logging.getLogger("zcad_mcp.workspace")

STATE_FILE = ".current_dxf.json"


class WorkspaceError(ValueError):
    """Invalid DXF path or workspace state."""


class Workspace:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.workspace_dir
        self._lock = threading.Lock()

    @property
    def state_file(self) -> Path:
        return self.root / STATE_FILE

    def resolve_dxf_path(self, path: str | None, *, must_exist: bool) -> Path:
        """Resolve a user-supplied DXF path. Relative paths are relative to the workspace."""
        if path is None or not str(path).strip():
            raise WorkspaceError("DXF path is empty")
        candidate = Path(str(path).strip()).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        if candidate.suffix.lower() != ".dxf":
            raise WorkspaceError(f"Only .dxf files are supported, got {candidate.name!r}")
        if must_exist and not candidate.is_file():
            raise WorkspaceError(f"DXF file not found: {candidate}")
        return candidate

    def current_dxf(self) -> Path:
        with self._lock:
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                value = data.get("path")
                if isinstance(value, str) and value:
                    return Path(value)
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                log.warning("Ignoring broken %s: %s", self.state_file, exc)
        return self.settings.default_dxf_path.resolve()

    def set_current_dxf(self, path: str | Path) -> Path:
        resolved = self.resolve_dxf_path(str(path), must_exist=True)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"path": str(resolved)}), encoding="utf-8")
            tmp.replace(self.state_file)
        log.info("current DXF set to %s", resolved)
        return resolved

    def describe(self, path: Path | None = None) -> dict[str, Any]:
        """``{path, exists, size, mtime}`` for ``path`` (default: current DXF)."""
        target = path or self.current_dxf()
        info: dict[str, Any] = {"path": str(target), "exists": target.is_file(), "size": None, "mtime": None}
        if info["exists"]:
            stat = target.stat()
            info["size"] = stat.st_size
            info["mtime"] = stat.st_mtime
        return info


def read_header_extents(path: Path, max_lines: int = 200_000) -> dict[str, float] | None:
    """Read ``$EXTMIN``/``$EXTMAX`` from the HEADER of an ASCII DXF without loading entities.

    Returns ``{"min_x", "min_y", "max_x", "max_y"}`` or ``None`` when the header has no
    valid extents (missing, binary DXF, or the "empty drawing" sentinel values).
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            values: dict[str, dict[int, float]] = {}
            current: str | None = None
            for _ in range(max_lines // 2):
                code = fh.readline()
                value = fh.readline()
                if not code or not value:
                    break
                code, value = code.strip(), value.strip()
                if code == "0" and value in ("ENDSEC", "EOF"):
                    break
                if code == "9":
                    current = value if value in ("$EXTMIN", "$EXTMAX") else None
                elif current and code in ("10", "20"):
                    values.setdefault(current, {})[int(code)] = float(value)
    except (OSError, ValueError):
        return None
    try:
        box = {
            "min_x": values["$EXTMIN"][10],
            "min_y": values["$EXTMIN"][20],
            "max_x": values["$EXTMAX"][10],
            "max_y": values["$EXTMAX"][20],
        }
    except KeyError:
        return None
    if not all(math.isfinite(v) and abs(v) < 1e19 for v in box.values()):
        return None
    if box["min_x"] > box["max_x"] or box["min_y"] > box["max_y"]:
        return None
    return box

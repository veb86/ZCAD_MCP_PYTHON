"""HTTP client for the existing ZCAD Bridge (HTTPServer started in ZCAD with ``HTTPStart``).

Contract of the existing server (``veb86/zcadvelecAI``, ``cad_source/zcad/velec/IPCserver``;
analysed in ``veb86/ZCAD_MCP_SERVER/docs/httpserver-analysis.md``):

* ``GET  /ipc`` -> ``{"status": "ok", "message": "ZCAD HTTP server is available"}``
* ``POST /ipc`` with ``{"id": "<uuid>", "cmd": "<COMMAND>", "args": [...], "token"?: "..."}``
  -> ``{"id", "status": "ok", "result"?: "<string>"}`` or ``{"id", "status": "error", "error"}``.
  Command errors usually come with HTTP 200; early dispatcher errors may have an empty id.

Commands used by this client:

* ``EXPORT [path.dxf]`` - saves the current drawing as DXF (configurable via
  ``ZCAD_EXPORT_COMMAND``; a dedicated "export selection" command is to be agreed
  with the Bridge - the existing ``EXPORT`` writes the whole drawing);
* ``BEGIN_BATCH`` / ``BATCH_LINES [[[x1,y1,x2,y2], ...]]`` / ``END_BATCH`` - lines in one
  undo step (``ZCAD_DRAW_MODE=batch``, default);
* ``LINE [x1,y1,x2,y2]`` - one line per request (``ZCAD_DRAW_MODE=line``).

``POLYLINE`` is a stub in the current Bridge ("not implemented yet"), so polylines
are drawn as their line segments. The Bridge has no layer/colour arguments: the
entities get the current layer/colour of the drawing and a warning says so.
The MCP tool semantics stay the same - only this module knows Bridge details.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx

from config import Settings

log = logging.getLogger("zcad_mcp.zcad")

LAYER_COLOR_WARNING = (
    "The current ZCAD Bridge has no layer/color arguments: entities were created on the "
    "current layer with the current color of the drawing (requested layer={layer!r}, color={color})."
)


class ZcadBridgeError(Exception):
    """Bridge failure with a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"ok": False, "error": self.message, "error_code": self.code}
        if self.http_status is not None:
            data["http_status"] = self.http_status
        return data


@dataclass(frozen=True)
class Segment:
    x1: float
    y1: float
    x2: float
    y2: float

    def as_args(self) -> list[float]:
        return [self.x1, self.y1, self.x2, self.y2]


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{what} must be finite")
    return value


def parse_lines(lines: Any) -> list[Segment]:
    """Accept ``[{x1,y1,x2,y2}, ...]`` (also ``[[x1,y1,x2,y2], ...]``)."""
    if not isinstance(lines, list):
        raise ValueError("lines must be a JSON array of {x1,y1,x2,y2} objects")
    segments = []
    for index, item in enumerate(lines):
        if isinstance(item, dict):
            try:
                values = [item[k] for k in ("x1", "y1", "x2", "y2")]
            except KeyError as exc:
                raise ValueError(f"lines[{index}] lacks key {exc.args[0]!r}") from None
        elif isinstance(item, (list, tuple)) and len(item) == 4:
            values = list(item)
        else:
            raise ValueError(f"lines[{index}] must be an object {{x1,y1,x2,y2}}")
        segments.append(Segment(*(_number(v, f"lines[{index}]") for v in values)))
    return segments


def parse_polylines(polylines: Any) -> list[tuple[list[tuple[float, float]], bool]]:
    """Accept ``[{points: [[x,y], ...], closed?: bool}, ...]``."""
    if not isinstance(polylines, list):
        raise ValueError("polylines must be a JSON array of {points, closed?} objects")
    result = []
    for index, item in enumerate(polylines):
        if not isinstance(item, dict) or not isinstance(item.get("points"), list):
            raise ValueError(f"polylines[{index}] must be an object with a 'points' array")
        points = []
        for pindex, point in enumerate(item["points"]):
            if isinstance(point, dict):
                point = [point.get("x"), point.get("y")]
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                raise ValueError(f"polylines[{index}].points[{pindex}] must be [x, y]")
            points.append((_number(point[0], "x"), _number(point[1], "y")))
        if len(points) < 2:
            raise ValueError(f"polylines[{index}] needs at least 2 points")
        closed = item.get("closed", False)
        if not isinstance(closed, bool):
            raise ValueError(f"polylines[{index}].closed must be a boolean")
        result.append((points, closed))
    return result


def polyline_segments(points: Sequence[tuple[float, float]], closed: bool) -> list[Segment]:
    pts = list(points)
    if closed and len(pts) > 2 and pts[0] != pts[-1]:
        pts.append(pts[0])
    return [Segment(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:]) if a != b]


class ZcadClient:
    """Async client; one instance per server (shares the HTTP connection pool)."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.bridge_url,
            timeout=httpx.Timeout(settings.bridge_timeout_sec, connect=min(5.0, settings.bridge_timeout_sec)),
            transport=transport,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ low level
    async def _request(self, method: str, json_body: Any = None) -> Any:
        started = time.monotonic()
        try:
            response = await self._client.request(method, "/ipc", json=json_body)
        except httpx.TimeoutException as exc:
            raise ZcadBridgeError(
                "TIMEOUT",
                f"ZCAD Bridge did not answer within {self.settings.bridge_timeout_sec:g} s; "
                "the command may still be executed later by ZCAD",
            ) from exc
        except httpx.HTTPError as exc:
            raise ZcadBridgeError(
                "CONNECTION_ERROR",
                f"Cannot reach ZCAD Bridge at {self.settings.bridge_url} ({type(exc).__name__}: {exc}). "
                "Start it in ZCAD with HTTPStart and check ZCAD_BRIDGE_URL.",
            ) from exc
        finally:
            log.debug("bridge %s /ipc in %.3fs", method, time.monotonic() - started)
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            detail = data.get("error") if isinstance(data, dict) else None
            raise ZcadBridgeError(
                "HTTP_ERROR",
                f"ZCAD Bridge answered HTTP {response.status_code}" + (f": {detail}" if detail else ""),
                http_status=response.status_code,
            )
        if not isinstance(data, dict):
            raise ZcadBridgeError("INVALID_RESPONSE", "ZCAD Bridge answered with non-JSON or non-object body")
        return data

    async def execute(self, command: str, args: list[Any] | None = None) -> str:
        """Run one Bridge command; returns the ``result`` string or raises :class:`ZcadBridgeError`."""
        request_id = str(uuid.uuid4())
        body: dict[str, Any] = {"id": request_id, "cmd": command, "args": list(args or [])}
        if self.settings.bridge_token:
            body["token"] = self.settings.bridge_token
        data = await self._request("POST", body)
        status = data.get("status")
        response_id = data.get("id")
        if status not in ("ok", "error"):
            raise ZcadBridgeError("INVALID_RESPONSE", f"Unexpected Bridge status {status!r} for {command}")
        if response_id != request_id and not (status == "error" and response_id in ("", None)):
            raise ZcadBridgeError("INVALID_RESPONSE", "Bridge response id does not match the request id")
        if status == "error":
            raise ZcadBridgeError("COMMAND_ERROR", f"{command}: {data.get('error') or 'unknown error'}")
        result = data.get("result")
        return "" if result is None else str(result)

    # ------------------------------------------------------------------ tools
    async def status(self) -> dict[str, Any]:
        try:
            data = await self._request("GET")
        except ZcadBridgeError as exc:
            log.warning("zcad_status: %s", exc.message)
            return {"ok": False, "status": "unavailable", "detail": exc.message, "error_code": exc.code, "url": self.settings.bridge_url}
        ok = data.get("status") == "ok"
        return {"ok": ok, "status": str(data.get("status")), "detail": data.get("message"), "url": self.settings.bridge_url}

    async def export_dxf(self, out_path: Path) -> dict[str, Any]:
        """Ask ZCAD to write a DXF to ``out_path`` and wait until the file is complete."""
        before = out_path.stat().st_mtime_ns if out_path.exists() else None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        message = await self.execute(self.settings.export_command, [str(out_path)])
        await self._wait_for_file(out_path, before)
        return {"ok": True, "path": str(out_path), "bridge_result": message}

    async def _wait_for_file(self, path: Path, previous_mtime_ns: int | None) -> None:
        deadline = time.monotonic() + self.settings.export_wait_sec
        last_size = -1
        while True:
            if path.is_file():
                stat = path.stat()
                fresh = previous_mtime_ns is None or stat.st_mtime_ns != previous_mtime_ns
                if fresh and stat.st_size > 0 and stat.st_size == last_size:
                    return  # size is stable between two polls
                last_size = stat.st_size if fresh else -1
            if time.monotonic() >= deadline:
                raise ZcadBridgeError(
                    "EXPORT_TIMEOUT",
                    f"ZCAD accepted the export command but {path} was not written within "
                    f"{self.settings.export_wait_sec:g} s (ZCAD_EXPORT_WAIT_SEC)",
                )
            await asyncio.sleep(0.2)

    async def draw_segments(self, segments: list[Segment]) -> int:
        """Draw segments using the configured mode; returns the number of drawn lines."""
        if not segments:
            return 0
        if len(segments) > self.settings.max_draw_lines:
            raise ValueError(f"Too many lines: {len(segments)} > {self.settings.max_draw_lines} (ZCAD_MCP_MAX_DRAW_LINES)")
        if self.settings.draw_mode == "line":
            drawn = 0
            for segment in segments:
                try:
                    await self.execute("LINE", segment.as_args())
                except ZcadBridgeError as exc:
                    exc.message = f"{exc.message} (after {drawn} of {len(segments)} lines were drawn)"
                    raise
                drawn += 1
            return drawn

        await self.execute("BEGIN_BATCH", [])
        sent = 0
        error: ZcadBridgeError | None = None
        try:
            for chunk in _chunks(segments, self.settings.batch_chunk_size):
                await self.execute("BATCH_LINES", [[s.as_args() for s in chunk]])
                sent += len(chunk)
        except ZcadBridgeError as exc:
            error = exc
        # Always leave batch mode: ZCAD keeps the batch state globally.
        try:
            await self.execute("END_BATCH", [])
        except ZcadBridgeError as exc:
            error = error or exc
        if error is not None:
            error.message = f"{error.message} ({sent} of {len(segments)} lines were sent before the failure)"
            raise error
        return sent

    async def draw_lines(self, lines: Any, layer: str, color: int) -> dict[str, Any]:
        segments = parse_lines(lines)
        drawn = await self.draw_segments(segments)
        return {"ok": True, "drawn_count": drawn, "warnings": [LAYER_COLOR_WARNING.format(layer=layer, color=color)]}

    async def draw_polylines(self, polylines: Any, layer: str, color: int) -> dict[str, Any]:
        parsed = parse_polylines(polylines)
        segments: list[Segment] = []
        for points, closed in parsed:
            segments.extend(polyline_segments(points, closed))
        drawn = await self.draw_segments(segments)
        return {
            "ok": True,
            "drawn_count": len(parsed),
            "segments_drawn": drawn,
            "warnings": [
                "The current ZCAD Bridge POLYLINE command is not implemented; each polyline was drawn as LINE segments.",
                LAYER_COLOR_WARNING.format(layer=layer, color=color),
            ],
        }


def _chunks(items: Sequence[Segment], size: int) -> Iterable[Sequence[Segment]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]

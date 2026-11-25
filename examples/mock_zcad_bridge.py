"""Mock of the ZCAD Bridge (``/ipc`` of ZCAD HTTPServer) for tests and manual checks.

Implements the subset used by the MCP server with the same envelope as ZCAD:

* ``GET /ipc`` -> ``{"status": "ok", "message": "ZCAD HTTP server is available"}``
* ``POST /ipc`` ``{"id", "cmd", "args"}`` -> ``{"id", "status": "ok", "result"}`` or
  ``{"id", "status": "error", "error"}`` (HTTP 200, like ZCAD).
* ``EXPORT [path]`` writes a sample floor plan (double-line walls) with ezdxf;
* ``LINE``, ``BEGIN_BATCH``, ``BATCH_LINES``, ``END_BATCH`` record the drawn lines;
* ``POLYLINE`` answers "not implemented yet" like the current ZCAD build.

Manual run (then point the MCP server at it)::

    python examples/mock_zcad_bridge.py --port 5000
    ZCAD_BRIDGE_URL=http://127.0.0.1:5000 python server.py
"""

from __future__ import annotations

import argparse
import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import ezdxf
import ezdxf.bbox
import httpx


def write_sample_plan(path: str, thickness: float = 200.0, origin: tuple[float, float] = (1000.0, 500.0)) -> None:
    """A 6000 x 4000 plan with one inner wall at ``origin``; every wall is two parallel lines."""
    doc = ezdxf.new("R2010")
    doc.layers.add("WALLS")
    msp = doc.modelspace()
    t = thickness
    ox, oy = origin

    def rect(x1, y1, x2, y2):
        pts = [(ox + x1, oy + y1), (ox + x2, oy + y1), (ox + x2, oy + y2), (ox + x1, oy + y2), (ox + x1, oy + y1)]
        for a, b in zip(pts, pts[1:]):
            msp.add_line(a, b, dxfattribs={"layer": "WALLS"})

    rect(0, 0, 6000, 4000)  # outer contour
    rect(t, t, 3000 - t / 2, 4000 - t)  # left room (inner contour)
    rect(3000 + t / 2, t, 6000 - t, 4000 - t)  # right room
    # noise: furniture, text
    msp.add_circle((ox + 1500, oy + 2000), 300, dxfattribs={"layer": "FURNITURE"})
    msp.add_text("Room 1", dxfattribs={"layer": "TEXT", "insert": (ox + 1000, oy + 1000)})
    # ZCAD writes real $EXTMIN/$EXTMAX; ezdxf copies them from the modelspace on save.
    box = ezdxf.bbox.extents(msp)
    msp.dxf.extmin, msp.dxf.extmax = box.extmin, box.extmax
    doc.saveas(path)


class MockZcadBridge:
    """In-memory Bridge; thread-safe enough for tests and a manual HTTP server."""

    def __init__(self, *, export_writer=write_sample_plan, fail_commands: dict[str, str] | None = None):
        self.export_writer = export_writer
        self.fail_commands = dict(fail_commands or {})
        self.requests: list[dict[str, Any]] = []
        self.lines: list[list[float]] = []
        self.in_batch = False
        self.batches = 0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- protocol
    def handle(self, method: str, body: Any) -> tuple[int, dict[str, Any]]:
        if method == "GET":
            return 200, {"status": "ok", "message": "ZCAD HTTP server is available"}
        if not isinstance(body, dict) or not isinstance(body.get("cmd"), str):
            return 200, {"id": "", "status": "error", "error": "Invalid request"}
        request_id = str(body.get("id", ""))
        cmd = body["cmd"].upper()
        args = body.get("args") or []
        with self._lock:
            self.requests.append({"cmd": cmd, "args": args})
            try:
                if cmd in self.fail_commands:
                    raise RuntimeError(self.fail_commands[cmd])
                result = self._command(cmd, args)
            except Exception as exc:  # noqa: BLE001 - mirrors ZCAD error answers
                return 200, {"id": request_id, "status": "error", "error": str(exc)}
        answer = {"id": request_id, "status": "ok"}
        if result is not None:
            answer["result"] = result
        return 200, answer

    def _command(self, cmd: str, args: list[Any]) -> str | None:
        if cmd == "PING":
            return "pong"
        if cmd == "EXPORT":
            if len(args) != 1 or not str(args[0]).lower().endswith(".dxf"):
                raise ValueError("EXPORT expects [path.dxf]")
            self.export_writer(str(args[0]))
            return f"Exported to {args[0]}"
        if cmd == "LINE":
            self.lines.append(self._segment(args))
            return "Line created"
        if cmd == "BEGIN_BATCH":
            if self.in_batch:
                raise RuntimeError("Batch already started")
            self.in_batch = True
            return "Batch started"
        if cmd == "BATCH_LINES":
            if not self.in_batch:
                raise RuntimeError("BATCH_LINES outside of a batch")
            segments = [self._segment(s) for s in args[0]]
            self.lines.extend(segments)
            return f"{len(segments)} lines added"
        if cmd == "END_BATCH":
            if not self.in_batch:
                raise RuntimeError("No active batch")
            self.in_batch = False
            self.batches += 1
            return "Batch committed"
        if cmd in ("POLYLINE", "ARC"):
            raise NotImplementedError(f"{cmd} not implemented yet")
        raise ValueError(f"Unknown command: {cmd}")

    @staticmethod
    def _segment(args: Any) -> list[float]:
        if not isinstance(args, list) or len(args) != 4:
            raise ValueError("expected [x1, y1, x2, y2]")
        values = [float(v) for v in args]
        if not all(math.isfinite(v) for v in values):
            raise ValueError("coordinates must be finite")
        return values

    # ---------------------------------------------------------------- adapters
    def transport(self) -> httpx.MockTransport:
        """``httpx.MockTransport`` for in-process tests."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path != "/ipc":
                return httpx.Response(404, json={"status": "error", "error": "Not found"})
            body = json.loads(request.content) if request.content else None
            status, payload = self.handle(request.method, body)
            return httpx.Response(status, json=payload)

        return httpx.MockTransport(handler)

    def serve(self, host: str = "127.0.0.1", port: int = 5000) -> ThreadingHTTPServer:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.path != "/ipc":
                    return self._reply(404, {"status": "error", "error": "Not found"})
                self._reply(*bridge.handle("GET", None))

            def do_POST(self):  # noqa: N802
                if self.path != "/ipc":
                    return self._reply(404, {"status": "error", "error": "Not found"})
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw)
                except ValueError:
                    return self._reply(200, {"id": "", "status": "error", "error": "Invalid JSON"})
                self._reply(*bridge.handle("POST", body))

            def log_message(self, fmt, *args):  # keep stdout clean
                import sys

                sys.stderr.write("mock-bridge: " + fmt % args + "\n")

        return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    ns = parser.parse_args()
    bridge = MockZcadBridge()
    httpd = bridge.serve(ns.host, ns.port)
    print(f"Mock ZCAD Bridge on http://{ns.host}:{ns.port}/ipc (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"Lines drawn: {len(bridge.lines)}")


if __name__ == "__main__":
    main()

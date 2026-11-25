"""ZcadClient against an httpx.MockTransport (no real ZCAD needed)."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import httpx
import pytest

from zcad_client import ZcadBridgeError, ZcadClient, parse_lines, parse_polylines, polyline_segments


def run(coro):
    return asyncio.run(coro)


def client_for(settings, bridge=None, handler=None, **overrides):
    settings = dataclasses.replace(settings, **overrides)
    transport = bridge.transport() if bridge is not None else httpx.MockTransport(handler)
    return ZcadClient(settings, transport=transport)


def test_status_ok(settings, bridge):
    answer = run(client_for(settings, bridge).status())
    assert answer["ok"] is True and answer["status"] == "ok"
    assert answer["detail"] == "ZCAD HTTP server is available"


def test_status_unavailable(settings):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    answer = run(client_for(settings, handler=handler).status())
    assert answer["ok"] is False and answer["status"] == "unavailable"
    assert answer["error_code"] == "CONNECTION_ERROR" and "HTTPStart" in answer["detail"]


def test_execute_sends_envelope_and_token(settings):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"id": body["id"], "status": "ok", "result": "done"})

    client = client_for(settings, handler=handler, bridge_token="t0k")
    assert run(client.execute("LINE", [0, 0, 1, 1])) == "done"
    method, path, body = seen[0]
    assert (method, path) == ("POST", "/ipc")
    assert body["cmd"] == "LINE" and body["args"] == [0, 0, 1, 1] and body["token"] == "t0k"
    assert len(body["id"]) == 36


@pytest.mark.parametrize(
    "response, code",
    [
        (lambda b: httpx.Response(200, json={"id": b["id"], "status": "error", "error": "Unknown command"}), "COMMAND_ERROR"),
        (lambda b: httpx.Response(200, json={"id": "", "status": "error", "error": "Invalid JSON"}), "COMMAND_ERROR"),
        (lambda b: httpx.Response(200, json={"id": "other", "status": "ok"}), "INVALID_RESPONSE"),
        (lambda b: httpx.Response(200, json={"id": b["id"], "status": "maybe"}), "INVALID_RESPONSE"),
        (lambda b: httpx.Response(200, text="<html>"), "INVALID_RESPONSE"),
        (lambda b: httpx.Response(500, json={"status": "error", "error": "boom"}), "HTTP_ERROR"),
    ],
)
def test_execute_errors(settings, response, code):
    client = client_for(settings, handler=lambda request: response(json.loads(request.content)))
    with pytest.raises(ZcadBridgeError) as info:
        run(client.execute("LINE", [0, 0, 1, 1]))
    assert info.value.code == code


def test_execute_timeout_is_not_retried(settings):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(ZcadBridgeError) as info:
        run(client_for(settings, handler=handler).execute("LINE", [0, 0, 1, 1]))
    assert info.value.code == "TIMEOUT" and len(calls) == 1


# ------------------------------------------------------------------ drawing
def test_parse_lines_formats():
    segs = parse_lines([{"x1": 0, "y1": 0, "x2": 1, "y2": 2, "length": 3}, [1, 1, 2, 2]])
    assert [s.as_args() for s in segs] == [[0, 0, 1, 2], [1, 1, 2, 2]]
    for bad in ({}, [{"x1": 0}], [[0, 0, 1]], [{"x1": "a", "y1": 0, "x2": 0, "y2": 0}], [[0, 0, float("nan"), 1]], [[True, 0, 1, 1]]):
        with pytest.raises(ValueError):
            parse_lines(bad)


def test_polyline_segments():
    (points, closed), = parse_polylines([{"points": [[0, 0], {"x": 10, "y": 0}, [10, 10]], "closed": True}])
    segs = polyline_segments(points, closed)
    assert [s.as_args() for s in segs] == [[0, 0, 10, 0], [10, 0, 10, 10], [10, 10, 0, 0]]
    for bad in ([{"points": [[0, 0]]}], [{"points": [[0, 0], [1, 1]], "closed": "yes"}], [[0, 0, 1, 1]]):
        with pytest.raises(ValueError):
            parse_polylines(bad)


def test_draw_lines_batch_mode_chunks(settings, bridge):
    client = client_for(settings, bridge, batch_chunk_size=2)
    lines = [{"x1": i, "y1": 0, "x2": i, "y2": 10} for i in range(5)]
    answer = run(client.draw_lines(lines, "IM_ANALYSIS", 1))
    assert answer["ok"] is True and answer["drawn_count"] == 5
    assert [r["cmd"] for r in bridge.requests] == ["BEGIN_BATCH", "BATCH_LINES", "BATCH_LINES", "BATCH_LINES", "END_BATCH"]
    assert bridge.lines[4] == [4, 0, 4, 10] and bridge.batches == 1 and not bridge.in_batch
    assert "layer" in answer["warnings"][0]


def test_draw_lines_line_mode(settings, bridge):
    client = client_for(settings, bridge, draw_mode="line")
    answer = run(client.draw_lines([[0, 0, 1, 1], [1, 1, 2, 2]], "L", 3))
    assert answer["drawn_count"] == 2
    assert [r["cmd"] for r in bridge.requests] == ["LINE", "LINE"]


def test_batch_is_closed_after_failure(settings):
    from mock_zcad_bridge import MockZcadBridge

    bridge = MockZcadBridge(fail_commands={"BATCH_LINES": "GUI busy"})
    client = client_for(settings, bridge)
    with pytest.raises(ZcadBridgeError) as info:
        run(client.draw_lines([[0, 0, 1, 1]], "L", 1))
    assert "GUI busy" in info.value.message and "0 of 1" in info.value.message
    assert bridge.requests[-1]["cmd"] == "END_BATCH" and not bridge.in_batch


def test_draw_limit(settings, bridge):
    client = client_for(settings, bridge, max_draw_lines=3)
    with pytest.raises(ValueError, match="Too many lines"):
        run(client.draw_lines([[0, 0, 1, 1]] * 4, "L", 1))
    assert bridge.requests == []


def test_draw_polylines_as_segments(settings, bridge):
    client = client_for(settings, bridge)
    polylines = [{"points": [[0, 0], [10, 0], [10, 10]], "closed": True}, {"points": [[0, 0], [5, 5]]}]
    answer = run(client.draw_polylines(polylines, "IM_WALLS", 1))
    assert answer["drawn_count"] == 2 and answer["segments_drawn"] == 4
    assert len(bridge.lines) == 4 and "POLYLINE" not in [r["cmd"] for r in bridge.requests]


# ------------------------------------------------------------------ export
def test_export_waits_for_file(settings, bridge):
    settings.ensure_dirs()
    target = settings.workspace_dir / "selection.dxf"
    answer = run(client_for(settings, bridge).export_dxf(target))
    assert answer["ok"] is True and target.stat().st_size > 0
    assert bridge.requests == [{"cmd": "EXPORT", "args": [str(target)]}]


def test_export_custom_command(settings, bridge):
    settings.ensure_dirs()
    bridge.fail_commands = {}
    target = settings.workspace_dir / "x.dxf"
    client = client_for(settings, bridge, export_command="PING")  # PING "succeeds" but writes nothing
    with pytest.raises(ZcadBridgeError) as info:
        run(client.export_dxf(target))
    assert info.value.code == "EXPORT_TIMEOUT"
    assert bridge.requests[0]["cmd"] == "PING"

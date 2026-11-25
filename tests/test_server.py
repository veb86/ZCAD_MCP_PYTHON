"""MCP-level tests: the server is driven by an in-process MCP client and a mock ZCAD Bridge."""

from __future__ import annotations

import asyncio
import json

from mcp import Client

from server import build_server

EXPECTED_TOOLS = {
    "list_skills", "get_skill_source", "get_skill_template", "create_skill", "update_skill",
    "delete_skill", "run_skill", "get_current_dxf", "set_current_dxf", "zcad_status",
    "zcad_export_selection", "zcad_draw_lines", "zcad_draw_polylines",
}


def payload(result):
    assert not result.is_error, result
    if result.structured_content and set(result.structured_content) == {"result"}:
        return result.structured_content["result"]
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


def session(settings, bridge, scenario):
    server = build_server(settings, transport=bridge.transport())

    async def main():
        async with Client(server) as client:
            async def call(tool, /, **args):
                return payload(await client.call_tool(tool, args))

            return await scenario(client, call)

    return asyncio.run(main())


def test_tools_are_listed(settings, bridge):
    async def scenario(client, call):
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == EXPECTED_TOOLS
        run_skill = tools["run_skill"].input_schema
        assert run_skill["required"] == ["name"]
        assert set(run_skill["properties"]) == {"name", "params_json", "dxf_path", "max_segments"}
        assert tools["zcad_draw_lines"].input_schema["properties"]["layer"]["default"] == "IM_ANALYSIS"
        assert tools["zcad_draw_polylines"].input_schema["properties"]["layer"]["default"] == "IM_WALLS"
        assert all(t.description for t in tools.values())

    session(settings, bridge, scenario)


def test_skill_tools(settings, bridge, detect_walls_source):
    async def scenario(client, call):
        skills = await call("list_skills")
        assert {s["name"] for s in skills} == {"extract_line_stats", "find_parallel_lines"}
        assert all(set(s) >= {"name", "description", "params_schema"} for s in skills)

        source = await call("get_skill_source", name="find_parallel_lines")
        assert source["ok"] and "def run(" in source["source"] and source["description"]
        template = await call("get_skill_template")
        assert "def run(" in template["source"] and "max_segments" in template["source"]

        missing = await call("get_skill_source", name="nope")
        assert missing["ok"] is False and "not found" in missing["error"]
        bad_name = await call("create_skill", name="../evil", source_code="def run(d, p): return {}")
        assert bad_name["ok"] is False and "Invalid skill name" in bad_name["error"]
        forbidden = await call("create_skill", name="evil", source_code="import socket\ndef run(d, p): return {}")
        assert forbidden["ok"] is False and forbidden["errors"]

        created = await call("create_skill", name="detect_walls", source_code=detect_walls_source)
        assert created["ok"] is True and created["path"].endswith("detect_walls.py")
        again = await call("create_skill", name="detect_walls", source_code=detect_walls_source)
        assert again["ok"] is False and "overwrite" in again["error"]
        updated = await call("update_skill", name="detect_walls", source_code=detect_walls_source, description="v2")
        assert updated["ok"] is True and updated["overwritten"] is True
        assert (await call("update_skill", name="ghost", source_code=detect_walls_source))["ok"] is False
        listed = {s["name"]: s for s in await call("list_skills")}
        assert listed["detect_walls"]["description"] == "v2"
        assert listed["detect_walls"]["params_schema"]["properties"]["min_thickness"]["type"] == "number"

        deleted = await call("delete_skill", name="detect_walls")
        assert deleted["ok"] is True
        assert "detect_walls" not in {s["name"] for s in await call("list_skills")}

    session(settings, bridge, scenario)


def test_run_skill_requires_dxf(settings, bridge, grid_dxf):
    async def scenario(client, call):
        current = await call("get_current_dxf")
        assert current["exists"] is False and current["path"].endswith("selection.dxf")
        no_dxf = await call("run_skill", name="extract_line_stats")
        assert no_dxf["ok"] is False and "zcad_export_selection" in no_dxf["error"]

        explicit = await call("run_skill", name="extract_line_stats", dxf_path="grid.dxf")
        assert explicit["ok"] is True and explicit["stats"]["segments_used"] == 503
        assert set(explicit) >= {"ok", "skill", "stats", "result", "warnings"}

        bad_json = await call("run_skill", name="extract_line_stats", dxf_path="grid.dxf", params_json="{oops")
        assert bad_json["ok"] is False and "params_json" in bad_json["error"]
        not_object = await call("run_skill", name="extract_line_stats", dxf_path="grid.dxf", params_json="[1]")
        assert not_object["ok"] is False
        not_dxf = await call("run_skill", name="extract_line_stats", dxf_path="/etc/passwd")
        assert not_dxf["ok"] is False and ".dxf" in not_dxf["error"]

        selected = await call("set_current_dxf", path=str(grid_dxf))
        assert selected["ok"] and selected["exists"] and selected["size"] > 0
        assert (await call("get_current_dxf"))["path"] == str(grid_dxf)
        limited = await call("run_skill", name="find_parallel_lines", params_json='{"angle_tol_deg": 0.5}', max_segments=5)
        assert limited["ok"] is True and len(limited["result"]["candidates"]) == 5

    session(settings, bridge, scenario)


def test_walls_from_selection_scenario(settings, bridge, detect_walls_source):
    """Acceptance scenario of the issue: export -> create skill -> run -> draw."""

    async def scenario(client, call):
        status = await call("zcad_status")
        assert status["ok"] is True and status["status"] == "ok"

        exported = await call("zcad_export_selection")
        assert exported["ok"] is True
        assert exported["path"] == str((settings.workspace_dir / "selection.dxf").resolve())
        assert exported["bbox"] == {"min_x": 1000.0, "min_y": 500.0, "max_x": 7000.0, "max_y": 4500.0}
        assert (await call("get_current_dxf"))["path"] == exported["path"]

        stats = await call("run_skill", name="extract_line_stats", params_json='{"layers": ["WALLS"]}')
        assert stats["ok"] is True and stats["stats"]["segments_used"] == 12

        assert (await call("create_skill", name="detect_walls", source_code=detect_walls_source))["ok"]
        walls = await call(
            "run_skill", name="detect_walls", params_json=json.dumps({"min_thickness": 150, "max_thickness": 250})
        )
        assert walls["ok"] is True, walls
        found = walls["result"]["walls"]
        assert walls["stats"]["walls_found"] == len(found) >= 6
        assert {w["thickness"] for w in found} == {200.0}

        drawn = await call("zcad_draw_polylines", polylines_json=json.dumps(found), layer="IM_WALLS", color=1)
        assert drawn["ok"] is True and drawn["drawn_count"] == len(found)
        assert len(bridge.lines) == len(found)

        cands = await call("run_skill", name="find_parallel_lines", max_segments=3)
        lines = await call("zcad_draw_lines", lines_json=json.dumps(cands["result"]["candidates"]))
        assert lines == {"ok": True, "drawn_count": 3, "warnings": lines["warnings"]}
        assert len(bridge.lines) == len(found) + 3

    session(settings, bridge, scenario)


def test_zcad_errors_are_structured(settings, bridge):
    from mock_zcad_bridge import MockZcadBridge

    failing = MockZcadBridge(fail_commands={"EXPORT": "Command EXPORT not registered"})

    async def scenario(client, call):
        exported = await call("zcad_export_selection", out_path="sub/part.dxf")
        assert exported["ok"] is False and exported["error_code"] == "COMMAND_ERROR"
        assert "not registered" in exported["error"]
        bad = await call("zcad_draw_lines", lines_json='[{"x1": 0}]')
        assert bad["ok"] is False and "x2" in bad["error"] or "y1" in bad["error"]
        not_dxf = await call("zcad_export_selection", out_path="part.txt")
        assert not_dxf["ok"] is False and ".dxf" in not_dxf["error"]
        assert failing.requests and failing.requests[0]["cmd"] == "EXPORT"

    session(settings, failing, scenario)


def test_bridge_down(settings):
    import httpx

    class Down:
        def transport(self):
            def handler(request):
                raise httpx.ConnectError("refused", request=request)

            return httpx.MockTransport(handler)

    async def scenario(client, call):
        status = await call("zcad_status")
        assert status["ok"] is False and status["status"] == "unavailable"
        drawn = await call("zcad_draw_lines", lines_json="[[0, 0, 1, 1]]")
        assert drawn["ok"] is False and drawn["error_code"] == "CONNECTION_ERROR"

    session(settings, Down(), scenario)

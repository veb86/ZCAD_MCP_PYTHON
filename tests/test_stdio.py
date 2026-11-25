"""End-to-end: start ``python server.py`` over stdio exactly like an MCP client does."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]


def test_stdio_server_lists_tools_and_runs_skill(settings, grid_dxf, tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py")],
        cwd=str(tmp_path),  # MCP clients start servers from arbitrary directories
        env={
            "ZCAD_MCP_WORKSPACE": str(settings.workspace_dir),
            "ZCAD_MCP_SKILLS": str(settings.skills_dir),
            "ZCAD_BRIDGE_URL": "http://127.0.0.1:9",  # nothing listens there
            "ZCAD_BRIDGE_TIMEOUT_SEC": "3",
            "ZCAD_MCP_ENV_FILE": str(tmp_path / "missing.env"),
        },
    )

    async def main():
        async with Client(params) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert {"list_skills", "run_skill", "create_skill", "zcad_export_selection", "zcad_draw_lines"} <= tools

            result = await client.call_tool("run_skill", {"name": "extract_line_stats", "dxf_path": str(grid_dxf)})
            answer = json.loads(result.content[0].text)
            assert answer["ok"] is True and answer["stats"]["segments_used"] == 503

            result = await client.call_tool("zcad_status", {})
            status = json.loads(result.content[0].text)
            assert status["ok"] is False and status["status"] == "unavailable"

    asyncio.run(asyncio.wait_for(main(), timeout=60))

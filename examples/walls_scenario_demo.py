"""End-to-end demo of the "walls from selection" scenario without ZCAD.

Starts the HTTP mock of the ZCAD Bridge (``mock_zcad_bridge.py``) on a free port,
launches ``server.py`` over stdio exactly like an MCP client (Claude Desktop,
Cursor) does, and plays the role of the AI:

    zcad_status -> zcad_export_selection -> run_skill(extract_line_stats)
    -> create_skill(detect_walls) -> run_skill(detect_walls) -> zcad_draw_polylines

Run: ``python examples/walls_scenario_demo.py`` (uses a temporary workspace).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
from pathlib import Path

from mcp import Client, StdioServerParameters

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from mock_zcad_bridge import MockZcadBridge  # noqa: E402


def short(data: object, limit: int = 600) -> str:
    text = json.dumps(data, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


async def scenario(bridge_url: str, tmp: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py")],
        env={
            "ZCAD_BRIDGE_URL": bridge_url,
            "ZCAD_MCP_WORKSPACE": str(tmp / "workspace"),
            "ZCAD_MCP_SKILLS": str(tmp / "skills"),
            "ZCAD_MCP_ENV_FILE": str(tmp / "none.env"),
        },
    )
    # Start with the bundled skills.
    (tmp / "skills").mkdir()
    for path in (ROOT / "skills").glob("*.py"):
        (tmp / "skills" / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    async with Client(params) as client:

        async def call(tool: str, /, **args):
            result = await client.call_tool(tool, args)
            if result.structured_content and set(result.structured_content) == {"result"}:
                data = result.structured_content["result"]
            else:
                data = json.loads(result.content[0].text)
            print(f"\n>>> {tool}({short(args, 120)})\n<<< {short(data)}")
            return data

        tools = (await client.list_tools()).tools
        print("tools:", ", ".join(t.name for t in tools))
        await call("zcad_status")
        await call("zcad_export_selection")
        await call("list_skills")
        await call("run_skill", name="extract_line_stats", params_json='{"layers": ["WALLS"]}')
        source = (HERE / "detect_walls_skill.py").read_text(encoding="utf-8")
        await call("create_skill", name="detect_walls", source_code=source, description="Walls from double lines")
        walls = await call(
            "run_skill", name="detect_walls", params_json='{"min_thickness": 150, "max_thickness": 250}'
        )
        await call("zcad_draw_polylines", polylines_json=json.dumps(walls["result"]["walls"]), layer="IM_WALLS")


def main() -> None:
    bridge = MockZcadBridge()
    httpd = bridge.serve("127.0.0.1", 0)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(url, Path(tmp)))
    finally:
        httpd.shutdown()
    print(f"\nMock ZCAD received commands: {[r['cmd'] for r in bridge.requests]}")
    print(f"Lines drawn in the mock drawing: {len(bridge.lines)}")


if __name__ == "__main__":
    main()

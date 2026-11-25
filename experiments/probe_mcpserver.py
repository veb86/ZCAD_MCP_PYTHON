"""Probe: how MCPServer (mcp v2) exposes dict-returning tools and runs sync tools."""
import asyncio
import threading

from mcp import Client
from mcp.server.mcpserver import MCPServer

server = MCPServer("probe")


@server.tool()
def sync_tool(x: int, note: str | None = None) -> dict:
    """Sync tool docstring."""
    return {"x": x, "thread": threading.current_thread().name, "note": note}


@server.tool()
def failing() -> dict:
    """Raises."""
    raise ValueError("boom")


async def main():
    async with Client(server) as client:
        tools = await client.list_tools()
        for t in tools.tools:
            print(t.name, t.input_schema, t.output_schema)
        r = await client.call_tool("sync_tool", {"x": 3})
        print("main thread:", threading.current_thread().name)
        print(r)
        r = await client.call_tool("failing", {})
        print(r)


asyncio.run(main())

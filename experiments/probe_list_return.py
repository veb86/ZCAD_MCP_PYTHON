"""Probe: how MCPServer serializes a tool returning list[dict]."""
import asyncio
from typing import Any
from mcp import Client
from mcp.server.mcpserver import MCPServer

server = MCPServer("probe")

@server.tool()
def items() -> list[dict[str, Any]]:
    """List."""
    return [{"a": 1}, {"b": 2}]

async def main():
    async with Client(server) as client:
        tools = await client.list_tools()
        print(tools.tools[0].output_schema)
        r = await client.call_tool("items", {})
        print(r)

asyncio.run(main())

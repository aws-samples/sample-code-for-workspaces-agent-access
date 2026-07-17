# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A tiny stdio MCP server used as a known-good reference in the test suite.

Run modes (selected by the first CLI arg, default "good"):
  good       - exposes two trivial tools ("echo", "ping"); the happy path.
  empty      - exposes no tools; exercises the zero-tools FAIL path.
  slow-init  - sleeps before serving, simulating a slow spawn + initialize
               (the service does not time this out, so the tester should
               WARN, not FAIL).
  slow-list  - answers tools/list slowly, simulating a cold first list that blows
               the 5 s budget (the tester should FAIL tools-list-latency).

An optional second arg overrides the delay in seconds (default 6, comfortably over
the 5 s budget). The simple modes use FastMCP; slow-list uses the low-level Server so
it can intercept the tools/list handler.
"""

import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "good"
delay = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0


def run_fastmcp(with_tools: bool) -> None:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("forwarding-stub-server")

    if with_tools:

        @mcp.tool()
        def echo(text: str) -> str:
            """Return the text it was given."""
            return text

        @mcp.tool()
        def ping() -> str:
            """Return 'pong', a no-argument tool for --probe-tools."""
            return "pong"

    mcp.run()


def run_slow_list(delay_s: float) -> None:
    import anyio
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    server = Server("forwarding-stub-server")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        # Simulate an expensive cold first listing.
        await anyio.sleep(delay_s)
        return [
            types.Tool(
                name="echo",
                description="Return the text it was given.",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        return [types.TextContent(type="text", text=str(arguments.get("text", "")))]

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)


if __name__ == "__main__":
    if mode == "slow-init":
        # Block before we start serving: spawn + initialize appears slow.
        time.sleep(delay)
        run_fastmcp(with_tools=True)
    elif mode == "slow-list":
        run_slow_list(delay)
    else:
        run_fastmcp(with_tools=(mode != "empty"))

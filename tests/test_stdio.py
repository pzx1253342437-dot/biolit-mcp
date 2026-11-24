"""Start the real server process and talk to it over stdio, the way Claude Desktop does."""

from __future__ import annotations

import os
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def test_server_speaks_mcp_over_stdio():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "biolit_mcp.server"],
        env={**os.environ, "BIOLIT_CACHE": "0"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "biolit"
        assert "Never cite a paper from memory" in (init.instructions or "")
        tools = await session.list_tools()
        assert len(tools.tools) == 5
        # Input validation happens before any network call.
        res = await session.call_tool("uniprot_entry", {"accession": "TP53"})
        assert res.is_error

"""Example MCP client: connect to a Streamable-HTTP MCP server, list tools, call one.

This module demonstrates the pattern used by ``mcp_runtime.py`` to talk to an
MCP server such as DeepWiki (``https://mcp.deepwiki.com/mcp``). It is a
standalone example and does not modify any project files.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.client.streamable_http import streamable_http_client
from mcp import ClientSession

logger = logging.getLogger(__name__)


async def list_mcp_tools(server_url: str) -> list[dict[str, Any]]:
    """Connect to an MCP server and return its advertised tools."""
    async with streamable_http_client(server_url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.inputSchema,
                }
                for tool in result.tools
            ]


async def call_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    """Connect to an MCP server and call a single tool with ``arguments``."""
    async with streamable_http_client(server_url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)
            return result


async def demo_deepwiki() -> None:
    """Walk through the DeepWiki MCP server end-to-end."""
    server_url = "https://mcp.deepwiki.com/mcp"

    print(f"Connecting to MCP server: {server_url}")
    tools = await list_mcp_tools(server_url)
    print(f"Discovered {len(tools)} tools:")
    for tool in tools:
        print(f"  - {tool['name']}: {tool['description'][:80]}")

    # Ask a question about an indexed repository.
    print("\nAsking DeepWiki about facebook/react...")
    try:
        answer = await call_mcp_tool(
            server_url,
            "ask_wiki_question",
            {
                "repoName": "facebook/react",
                "question": "What is the core reconciler architecture?",
            },
        )
        text = _extract_text(answer)
        print("Answer:", text[:500])
    except Exception as exc:  # noqa: BLE001
        print(f"DeepWiki call failed (server may not be reachable): {exc}")


def _extract_text(result: Any) -> str:
    """Pull human-readable text out of an MCP ``CallToolResult``."""
    content = getattr(result, "content", None) or result
    if isinstance(content, list):
        parts = []
        for item in content:
            text = getattr(item, "text", None) or str(item)
            parts.append(text)
        return "\n".join(parts)
    return str(content)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(demo_deepwiki())


if __name__ == "__main__":
    main()

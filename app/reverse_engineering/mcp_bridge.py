"""Expose retained investigation evidence over MCP stdio."""
from __future__ import annotations

import argparse
import os
from uuid import UUID

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from app.reverse_engineering.contracts import Investigation, MAX_REPORT_BYTES


def build_server(port: int, token: str | None) -> MCPServer:
    if not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535.')
    server = MCPServer('THESUS investigation evidence', version='1', subscriptions=False)
    headers = {'Authorization': f'Bearer {token}'} if token else {}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def get_investigation(session_id: UUID, investigation_id: UUID) -> Investigation:
        """Read one retained investigation from the local THESUS workspace."""
        url = f'http://127.0.0.1:{port}/sessions/{session_id}/investigations/{investigation_id}'
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
                async with client.stream('GET', url, headers=headers) as response:
                    if response.status_code != 200:
                        raise RuntimeError('The investigation is unavailable in this workspace session.')
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_REPORT_BYTES + 128 * 1024:
                            raise RuntimeError('The workspace response exceeds the evidence budget.')
                        body.extend(chunk)
            return Investigation.model_validate_json(body)
        except (httpx.HTTPError, ValidationError) as exc:
            raise RuntimeError('The local THESUS workspace did not return a valid investigation.') from exc

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description='Serve retained THESUS investigation evidence over MCP stdio.')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    build_server(args.port, os.getenv('WORKSPACE_ACCESS_TOKEN')).run('stdio')


if __name__ == '__main__':
    main()

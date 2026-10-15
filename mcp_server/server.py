"""
BOM Cost Optimization — MCP Server (Streamable HTTP transport)
Exposes the BOM agent as a credential-free MCP server.

Compatible with:
  - Joule Work Desktop Hub
  - Claude Desktop
  - Cursor / any MCP client

Authentication is handled entirely by the main BOM agent (DEFAULT_SAP_USERNAME /
DEFAULT_SAP_PASSWORD env vars on the main agent app). This server needs no credentials.
"""
from __future__ import annotations

import os
import uuid
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import TransportSecuritySettings

BOM_AGENT_URL = os.environ.get(
    "BOM_AGENT_URL",
    "https://bom-cost-optimization-agent.cfapps.us10.hana.ondemand.com",
).rstrip("/")

mcp = FastMCP(
    name="BOM Cost Optimization Agent",
    instructions=(
        "You have access to a SAP BOM Cost Optimization Agent connected to a live "
        "S/4HANA system (CIN_100). Use the ask_bom_agent tool to answer any question "
        "about material prices, BOM numbers, budget analysis, plant comparisons, "
        "and procurement data. Always pass the user's question as-is — the agent "
        "handles all entity extraction and SAP lookups internally."
    ),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def ask_bom_agent(query: str) -> str:
    """
    Ask the BOM Cost Optimization Agent a question about SAP materials.

    Supports:
    - Price lookup: "What is the price of DEMOCAR at plant 1710?"
    - BOM numbers: "What are the BOM numbers for F-10A?"
    - Budget analysis: "How many SEAT can I buy with 1 CR INR at plant 1710?"
    - Plant comparison: "Which plant has the cheapest DEMOCAR?"
    - Cross-plant: "Compare DEMOCAR prices across all plants"
    - Multi-material: "Prices of F-10A, F-10B, F-20A at plant 1720"

    Args:
        query: Natural language question about SAP BOM costs or materials.

    Returns:
        Answer from the live SAP S/4HANA system.
    """
    session_id = f"mcp-{uuid.uuid4().hex[:8]}"
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{BOM_AGENT_URL}/api/chat",
                json={"message": query, "contextId": session_id},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            return data.get("reply") or data.get("content") or data.get("message") or str(data)
    except httpx.TimeoutException:
        return "Request timed out. The SAP system may be slow — please try again."
    except httpx.HTTPStatusError as e:
        return f"Agent returned HTTP {e.response.status_code}. Please try again."
    except Exception as e:
        return f"Error connecting to BOM agent: {e}"


_http_app = mcp.streamable_http_app()

_CORS_HEADERS = [
    (b"access-control-allow-origin",  b"*"),
    (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
    (b"access-control-allow-headers", b"content-type, authorization, mcp-session-id"),
]


async def application(scope, receive, send):
    if scope["type"] == "lifespan":
        await _http_app(scope, receive, send)
        return

    if scope["type"] != "http":
        await _http_app(scope, receive, send)
        return

    path   = scope.get("path", "")
    method = scope.get("method", "GET").upper()

    if method == "OPTIONS":
        await send({"type": "http.response.start", "status": 204,
                    "headers": _CORS_HEADERS + [(b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})
        return

    if path == "/health":
        body = b'{"status":"ok","service":"bom-mcp-server","transport":"streamable-http"}'
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())] + _CORS_HEADERS})
        await send({"type": "http.response.body", "body": body})
        return

    new_scope = dict(scope)
    new_scope["path"]     = "/mcp"
    new_scope["raw_path"] = b"/mcp"

    async def cors_send(message):
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            headers.extend(_CORS_HEADERS)
            message = {**message, "headers": headers}
        await send(message)

    await _http_app(new_scope, receive, cors_send)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(application, host="0.0.0.0", port=port)

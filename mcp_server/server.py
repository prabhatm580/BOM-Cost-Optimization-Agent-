"""
BOM Cost Optimization — MCP Server
Exposes the BOM agent as an MCP server so any MCP-compatible client
(Joule Work Desktop Hub, Claude Desktop, Cursor, etc.) can use it.

Calls the existing deployed agent at BOM_AGENT_URL — no SAP credentials
needed here; those are handled by the main agent.
"""
from __future__ import annotations

import os
import uuid
import httpx
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount

# ── Config ─────────────────────────────────────────────────────────────────────
BOM_AGENT_URL = os.environ.get(
    "BOM_AGENT_URL",
    "https://bom-cost-optimization-agent.cfapps.us10.hana.ondemand.com",
).rstrip("/")

# ── MCP Server ─────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="BOM Cost Optimization Agent",
    instructions=(
        "You have access to a SAP BOM Cost Optimization Agent connected to a live "
        "S/4HANA system (CIN_100). Use the ask_bom_agent tool to answer any question "
        "about material prices, BOM numbers, budget analysis, plant comparisons, "
        "and procurement data. Always pass the user's question as-is — the agent "
        "handles all entity extraction and SAP lookups internally."
    ),
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


# ── Health check endpoint ──────────────────────────────────────────────────────
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "bom-mcp-server"})


# ── Combine MCP SSE app + health endpoint ─────────────────────────────────────
_mcp_app = mcp.sse_app()


async def application(scope, receive, send):
    """Root ASGI app: /health goes to health handler, everything else to MCP."""
    if scope.get("type") == "http" and scope.get("path") == "/health":
        req = Request(scope, receive)
        resp = await health(req)
        await resp(scope, receive, send)
    else:
        await _mcp_app(scope, receive, send)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    mcp.run(transport="sse", host="0.0.0.0", port=port)

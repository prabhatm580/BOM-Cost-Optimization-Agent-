"""
BOM Cost Optimization — MCP Server (Streamable HTTP transport)
Exposes the BOM agent as an MCP server compatible with:
  - Joule Work Desktop Hub
  - Claude Desktop
  - Cursor / any MCP client

Auto-authenticates with SAP CIN_100 using SAP_USERNAME / SAP_PASSWORD env vars.
All callers share one service-account session — no per-user login needed.
"""
from __future__ import annotations

import asyncio
import os
import uuid
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import TransportSecuritySettings

# ── Config ─────────────────────────────────────────────────────────────────────
BOM_AGENT_URL = os.environ.get(
    "BOM_AGENT_URL",
    "https://bom-cost-optimization-agent.cfapps.us10.hana.ondemand.com",
).rstrip("/")

SAP_USERNAME = os.environ.get("SAP_USERNAME", "")
SAP_PASSWORD = os.environ.get("SAP_PASSWORD", "")

# ── Session token cache (service-account auto-login) ──────────────────────────
_cached_token: str | None = None
_token_lock = asyncio.Lock()


async def _get_auth_token() -> str | None:
    """
    Returns a cached SAP auth token, refreshing it if needed.
    Uses SAP_USERNAME / SAP_PASSWORD env vars for service-account login.
    Returns None if no credentials are configured.
    """
    global _cached_token

    if not SAP_USERNAME or not SAP_PASSWORD:
        return None

    async with _token_lock:
        if _cached_token:
            return _cached_token

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{BOM_AGENT_URL}/api/login",
                    json={"username": SAP_USERNAME, "password": SAP_PASSWORD},
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code == 200:
                    _cached_token = resp.json().get("token")
                    return _cached_token
        except Exception:
            pass

    return None


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
    headers: dict[str, str] = {"Content-Type": "application/json"}

    # Auto-login with service account credentials if available
    token = await _get_auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{BOM_AGENT_URL}/api/chat",
                json={"message": query, "contextId": session_id},
                headers=headers,
            )
            # If token expired, clear cache and retry once
            if response.status_code == 401 and token:
                global _cached_token
                _cached_token = None
                new_token = await _get_auth_token()
                if new_token:
                    headers["Authorization"] = f"Bearer {new_token}"
                    response = await client.post(
                        f"{BOM_AGENT_URL}/api/chat",
                        json={"message": query, "contextId": session_id},
                        headers=headers,
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


# ── Build the streamable HTTP app (MCP standard transport) ────────────────────
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

    # CORS preflight
    if method == "OPTIONS":
        await send({"type": "http.response.start", "status": 204,
                    "headers": _CORS_HEADERS + [(b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})
        return

    # Health check
    if path == "/health":
        sap_auth = "configured" if SAP_USERNAME else "not_configured"
        body = (
            f'{{"status":"ok","service":"bom-mcp-server",'
            f'"transport":"streamable-http","sap_auth":"{sap_auth}"}}'
        ).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())] + _CORS_HEADERS})
        await send({"type": "http.response.body", "body": body})
        return

    # Route everything to MCP handler (rewrite path to /mcp)
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

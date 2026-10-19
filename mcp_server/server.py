"""
BOM Cost Optimization — MCP Server (Streamable HTTP transport)

Two-tool flow that Joule follows automatically:
  1. sap_login(username, password)  — authenticate with SAP CIN_100
  2. ask_bom_agent(query)           — query live BOM / cost data

Joule reads the system prompt (instructions=) and handles the credential
dialog itself — it asks the user for username/password, calls sap_login,
then calls ask_bom_agent with the original question.
"""
from __future__ import annotations

import os
import uuid
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import TransportSecuritySettings

BOM_AGENT_URL = os.environ.get(
    "BOM_AGENT_URL",
    "https://bom-cost-optimization-agent.cfapps.eu10-004.hana.ondemand.com",
).rstrip("/")

# ── Per-user session store  (username → bearer token) ─────────────────────────
# Multiple Joule users can be active simultaneously; each has their own token.
_sessions: dict[str, str] = {}
_active_user: str | None = None   # last successfully logged-in user


# ── MCP server ─────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="BOM Cost Optimization Agent",
    instructions=(
        "You are connected to a SAP BOM Cost Optimization Agent (live S/4HANA CIN_100 system).\n\n"
        "AUTHENTICATION RULE:\n"
        "Before answering any question that needs SAP data, you MUST call sap_login first.\n"
        "If the user has not logged in yet:\n"
        "  1. Ask for their SAP CIN_100 username.\n"
        "  2. Ask for their SAP CIN_100 password (mark it as sensitive).\n"
        "  3. Call sap_login(username=..., password=...) with the provided credentials.\n"
        "  4. Only after a successful login, call ask_bom_agent with the original question.\n\n"
        "If sap_login returns success, proceed immediately to call ask_bom_agent.\n"
        "If sap_login returns failure, tell the user their credentials are wrong and ask again.\n\n"
        "CAPABILITIES (after login):\n"
        "- Material price lookup: 'DEMOCAR ka price plant 1710 mein kya hai?'\n"
        "- BOM numbers: 'F-10A ke BOM numbers kya hain?'\n"
        "- Budget analysis: '1 CR INR mein plant 1710 se kitne SEAT kharid sakte hain?'\n"
        "- Plant comparison: 'Sabse sasta plant kaunsa hai DEMOCAR ke liye?'\n"
        "- Multi-material: 'F-10A, F-10B, F-20A ka price plant 1720 mein batao'\n\n"
        "Always pass the user's question verbatim to ask_bom_agent — the agent handles "
        "entity extraction and all SAP lookups internally."
    ),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def sap_login(username: str, password: str) -> str:
    """
    Authenticate with SAP CIN_100 system.

    Call this before ask_bom_agent. Validates credentials against the live
    SAP system and stores a session token for subsequent data queries.

    Args:
        username: SAP CIN_100 username (e.g. I764359)
        password: SAP CIN_100 password

    Returns:
        Success message with username, or error description.
    """
    global _active_user

    if not username or not password:
        return "❌ Username and password are both required."

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{BOM_AGENT_URL}/api/login",
                json={"username": username.strip(), "password": password},
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                token = resp.json().get("token")
                if token:
                    _sessions[username.strip()] = token
                    _active_user = username.strip()
                    return (
                        f"✅ Login successful. Welcome, {username.strip()}! "
                        f"You are now connected to SAP CIN_100. "
                        f"You can now ask about BOM costs and material prices."
                    )
                return "❌ Login succeeded but no token received. Please try again."
            elif resp.status_code == 401:
                return "❌ Invalid credentials. Please check your SAP CIN_100 username and password."
            elif resp.status_code == 503:
                return "❌ SAP CIN_100 system is currently unavailable. Please try again later."
            else:
                return f"❌ Login failed (HTTP {resp.status_code}). Please try again."
    except httpx.TimeoutException:
        return "❌ Connection to SAP timed out. Please try again."
    except Exception as e:
        return f"❌ Login error: {e}"


@mcp.tool()
async def ask_bom_agent(query: str) -> str:
    """
    Query the BOM Cost Optimization Agent for live SAP data.

    Requires a prior successful sap_login call. Uses the stored session
    token to authenticate all SAP S/4HANA API calls.

    Supports:
    - Price lookup: "What is the price of DEMOCAR at plant 1710?"
    - BOM numbers: "What are the BOM numbers for F-10A?"
    - Budget analysis: "How many SEAT can I buy with 1 CR INR at plant 1710?"
    - Plant comparison: "Which plant has the cheapest DEMOCAR?"
    - Multi-material: "Prices of F-10A, F-10B, F-20A at plant 1720"

    Args:
        query: Natural language question about SAP BOM costs or materials.

    Returns:
        Answer from the live SAP S/4HANA system.
    """
    # Resolve bearer token from active session
    token: str | None = None
    if _active_user:
        token = _sessions.get(_active_user)

    if not token:
        return (
            "⚠️ Not authenticated. Please call sap_login(username, password) "
            "with your SAP CIN_100 credentials first."
        )

    session_id = f"mcp-{uuid.uuid4().hex[:8]}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{BOM_AGENT_URL}/api/chat",
                json={"message": query, "contextId": session_id},
                headers=headers,
            )
            # Token expired → clear and ask user to re-login
            if response.status_code == 401:
                if _active_user in _sessions:
                    del _sessions[_active_user]
                return (
                    "⚠️ Your SAP session has expired. "
                    "Please call sap_login again with your credentials."
                )
            response.raise_for_status()
            data = response.json()
            return data.get("reply") or data.get("content") or data.get("message") or str(data)
    except httpx.TimeoutException:
        return "⏱️ Request timed out. The SAP system may be slow — please try again."
    except httpx.HTTPStatusError as e:
        return f"❌ Agent returned HTTP {e.response.status_code}. Please try again."
    except Exception as e:
        return f"❌ Error connecting to BOM agent: {e}"


# ── ASGI application (Streamable HTTP transport) ───────────────────────────────
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
        users = len(_sessions)
        body = (
            f'{{"status":"ok","service":"bom-mcp-server",'
            f'"transport":"streamable-http","active_sessions":{users}}}'
        ).encode()
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

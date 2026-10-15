"""MCP tool loader.

Owned indirection layer between agent code and the Agent Gateway.
All agent code imports get_mcp_tools from here.

Behaviour is controlled by the IBD_TESTING environment variable:

  Production (IBD_TESTING not set):
      Uses Agent Gateway client directly from the SDK to connect via mTLS.
      Credentials are loaded from the UMS volume mount (/etc/ums/credentials/credentials)
      or the AGW_CREDENTIALS_JSON environment variable.

  Local / test mode (IBD_TESTING=1):
      Reads mcp-mock.json from the directory containing this file's parent
      (i.e. <asset-root>/mcp-mock.json) and returns LangChain StructuredTool
      instances built from the mock data — no network calls.
"""

import json
import logging
import os
import time
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Optional

from sap_cloud_sdk.agentgateway import create_client
from pydantic import create_model
from langchain_core.tools import StructuredTool

from util import enhance_tool_description, enhance_tool_name, call_mcp_tool_with_retry

logger = logging.getLogger(__name__)

# Context variable to pass user token from request to tool execution
_user_token_context: ContextVar[str | None] = ContextVar('user_token', default=None)

# Reusable AGW client for connection pooling
_agw_client: Optional[Any] = None

# mcp-mock.json lives at the asset root (one level above app/)
_MOCK_FILE = Path(__file__).parent.parent / "mcp-mock.json"


def _build_mock_tools() -> list:
    """Build LangChain StructuredTool instances from mcp-mock.json."""
    if not _MOCK_FILE.exists():
        return []

    try:
        mock_data = json.loads(_MOCK_FILE.read_text())
    except Exception:
        logger.warning("Failed to parse mcp-mock.json at %s — returning empty tool list", _MOCK_FILE, exc_info=True)
        return []

    tools = []
    from langchain_core.tools import StructuredTool
    from pydantic import Field, create_model

    for _server_slug, server in mock_data.get("servers", {}).items():
        for tool_name, tool_def in server.get("tools", {}).items():
            description = tool_def.get("description", "")
            mock_response = tool_def.get("mock_response", {})
            input_schema = tool_def.get("input_schema", {})

            props = input_schema.get("properties", {})
            required_fields = set(input_schema.get("required", []))
            field_definitions: dict = {}
            for field_name, field_info in props.items():
                json_type = field_info.get("type", "string")
                if json_type == "integer":
                    python_type = int
                elif json_type == "number":
                    python_type = float
                elif json_type == "boolean":
                    python_type = bool
                else:
                    python_type = str

                if field_name in required_fields:
                    field_definitions[field_name] = (python_type, Field(description=field_info.get("description", "")))
                else:
                    field_definitions[field_name] = (python_type, Field(default=None, description=field_info.get("description", "")))

            args_schema = (
                create_model(f"{tool_name}_args", **field_definitions)
                if field_definitions
                else create_model(f"{tool_name}_args")
            )
            _response = json.dumps(mock_response)

            async def _coroutine(_resp=_response, **kwargs) -> str:
                return _resp

            tools.append(
                StructuredTool(
                    name=tool_name,
                    description=description,
                    args_schema=args_schema,
                    coroutine=_coroutine,
                    handle_tool_error=True,
                )
            )

    logger.info("Loaded %d mock MCP tool(s) from %s", len(tools), _MOCK_FILE)
    return tools


def _convert_mcp_tool_to_langchain(mcp_tool: Any, agw_client: Any) -> StructuredTool:
    """Convert an MCP tool to a LangChain StructuredTool."""
    if mcp_tool is None:
        raise ValueError("mcp_tool parameter cannot be None")

    async def run(**kwargs) -> str:
        _tok = _user_token_context.get()
        return await call_mcp_tool_with_retry(agw_client, mcp_tool, user_token=_tok, **kwargs)

    properties = mcp_tool.input_schema.get("properties", {})
    required = set(mcp_tool.input_schema.get("required", []))

    fields = {}
    for name, prop in properties.items():
        prop_type = prop.get("type", "string")
        python_type = str
        if prop_type == "integer":
            python_type = int
        elif prop_type == "number":
            python_type = float
        elif prop_type == "boolean":
            python_type = bool

        if name in required:
            fields[name] = (python_type, ...)
        else:
            fields[name] = (python_type | None, None)

    args_schema = create_model(f"{mcp_tool.name}_args", **fields) if fields else None

    enhanced_description = enhance_tool_description(mcp_tool)
    namespaced_tool_name = enhance_tool_name(mcp_tool)

    return StructuredTool.from_function(
        coroutine=run,
        name=namespaced_tool_name,
        description=enhanced_description,
        args_schema=args_schema,
        handle_tool_error=True,
    )


async def get_mcp_tools(user_token: str | None) -> list:
    """Return LangChain-compatible MCP tools.

    In local/test mode (IBD_TESTING=1): returns mock tools from mcp-mock.json.
    In production: uses Agent Gateway client directly from SDK to connect via mTLS.
    """
    global _agw_client

    if os.environ.get("IBD_TESTING") == "1":
        return _build_mock_tools()

    if not user_token:
        raise ValueError("user_token is required for listing and calling MCP tools")

    try:
        if _agw_client is None:
            _agw_client = create_client()
            logger.info("Agent Gateway client created successfully")

        agw_client = _agw_client

        logger.info("Listing MCP tools with user credentials")
        _ut = user_token
        mcp_tools = await agw_client.list_mcp_tools(user_token=_ut)
        if not mcp_tools:
            logger.warning("Agent Gateway returned 0 tools - MCP servers may not be available")
            return []

        logger.info(f"Successfully retrieved {len(mcp_tools)} tool(s) from Agent Gateway")

        langchain_tools = []
        for mcp_tool in mcp_tools:
            try:
                langchain_tool = _convert_mcp_tool_to_langchain(mcp_tool, agw_client)
                langchain_tools.append(langchain_tool)
            except Exception as e:
                logger.warning(f"Failed to convert tool '{mcp_tool.name}': {e}")

        if not langchain_tools:
            logger.warning("No tools were successfully converted - returning empty list")
            return []

        return langchain_tools

    except Exception as e:
        logger.exception("Failed to load MCP tools from Agent Gateway")
        _agw_client = None
        return []


def set_user_token(user_token: str | None) -> Token:
    """Set the user token for MCP tool calls in the current async context."""
    if user_token:
        logger.debug("User token set for tool execution")
    else:
        logger.debug("User token cleared for tool execution")
    return _user_token_context.set(user_token)


def get_user_token() -> str | None:
    """Get the current user token from the async context."""
    return _user_token_context.get()

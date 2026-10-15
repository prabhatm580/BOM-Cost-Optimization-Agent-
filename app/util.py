"""
Utility functions for MCP tool processing.

Provides helper functions for enhancing MCP tool descriptions and metadata.
"""
import asyncio
import hashlib
import logging
import os
import re
from typing import Any

import httpx
from langchain_core.tools import ToolException

logger = logging.getLogger(__name__)

_MCP_RETRY_ATTEMPTS = 4
_MCP_RETRY_DELAY = 4.0  # seconds
# Maximum response size to prevent OOM - truncate responses larger than this
MCP_MAX_RESPONSE_CHARS = int(os.environ.get("MCP_MAX_RESPONSE_CHARS", 100_000))


def _is_retryable_error(exc: Exception) -> bool:
    """Return True for transient errors that are worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code < 400 or exc.response.status_code >= 500
    if isinstance(exc, (ExceptionGroup, BaseExceptionGroup)):
        return True
    return True


def enhance_tool_description(mcp_tool: Any) -> str:
    """Enhance MCP tool description with server name prefix."""
    if mcp_tool is None:
        logger.warning("enhance_tool_description called with None tool")
        return ""
    server_label = getattr(mcp_tool, "fragment_name", mcp_tool.server_name)
    return f"[{server_label}] {mcp_tool.description or ''}".strip()


def enhance_tool_name(mcp_tool: Any) -> str:
    """Get enhanced and namespaced tool name, sanitized to match ^[a-zA-Z0-9-_]+$ and at most 64 chars."""
    if mcp_tool is None:
        logger.warning("enhance_tool_name called with None tool")
        return ""
    server_name = mcp_tool.server_name
    tool_name = mcp_tool.name
    segments = server_name.split(":")
    remaining = segments[2:] if len(segments) > 2 else segments
    server_part = "_".join(remaining)
    raw = f"{server_part}__{tool_name}"
    sanitized = re.sub(r"[^a-zA-Z0-9\-_]", "_", raw)
    if len(sanitized) <= 64:
        return sanitized
    suffix = hashlib.sha256(sanitized.encode()).hexdigest()[:8]
    return f"{sanitized[:55]}_{suffix}"


async def call_mcp_tool_with_retry(agw_client: Any, mcp_tool: Any, user_token: str | None = None, **kwargs: Any) -> str:
    """Call an MCP tool with retry logic and error handling."""
    logger.info(f"call_mcp_tool_with_retry START: tool={mcp_tool.name}, args={kwargs}")

    if mcp_tool is None:
        raise ValueError("Tool parameter cannot be None")

    last_exc: Exception | None = None
    for attempt in range(1 + _MCP_RETRY_ATTEMPTS):
        try:
            arg_keys = list(kwargs.keys()) if kwargs else []
            logger.info(f"Calling MCP tool '{mcp_tool.name}' via Agent Gateway with {len(arg_keys)} argument(s)")

            _call_result = None
            try:
                call_params = {"tool": mcp_tool, **kwargs}
                if user_token is not None:
                    call_params["user_token"] = user_token
                _call_result = await agw_client.call_mcp_tool(**call_params)
            except (ExceptionGroup, BaseExceptionGroup) as eg:
                if _call_result is None:
                    logger.warning(f"ExceptionGroup raised and no result captured for {mcp_tool.name}: {eg}")
                    raise

            if _call_result is None:
                raise RuntimeError(f"SDK call_mcp_tool returned None for {mcp_tool.name}")

            result = str(_call_result) if _call_result else ""

            if len(result) > MCP_MAX_RESPONSE_CHARS:
                logger.warning(f"Response from {mcp_tool.name} truncated from {len(result)} to {MCP_MAX_RESPONSE_CHARS} chars")
                result = result[:MCP_MAX_RESPONSE_CHARS] + "\n...[truncated]"

            logger.info(f"MCP tool '{mcp_tool.name}' returned successfully (response length: {len(result)} chars)")
            return result

        except Exception as e:
            if not _is_retryable_error(e):
                logger.exception(f"Non-retryable error calling {mcp_tool.name}")
                raise
            last_exc = e
            if attempt < _MCP_RETRY_ATTEMPTS:
                logger.warning(f"Error calling {mcp_tool.name} (attempt {attempt + 1}/{1 + _MCP_RETRY_ATTEMPTS}), retrying in {_MCP_RETRY_DELAY}s: {e}")
                await asyncio.sleep(_MCP_RETRY_DELAY)

    logger.exception(f"Failed to call {mcp_tool.name} after {1 + _MCP_RETRY_ATTEMPTS} attempts", exc_info=last_exc)
    raise ToolException(
        f"Tool '{mcp_tool.name}' failed after {1 + _MCP_RETRY_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc

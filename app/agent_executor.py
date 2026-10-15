"""
AgentExecutor — dual-mode (Joule Studio + Cloud Foundry).

When JOULE_RUNTIME=1: loads MCP tools from Agent Gateway.
When JOULE_RUNTIME=0 (CF): skips MCP, uses direct S/4HANA client tools.
"""

from __future__ import annotations

import logging
import os

from a2a.server.agent_execution import AgentExecutor as A2AAgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    InternalError,
    Part,
    TaskState,
    TextPart,
    UnsupportedOperationError,
)
from a2a.utils import new_agent_text_message, new_task
from a2a.utils.errors import ServerError

from agent import SampleAgent
from load_skill_resources import get_load_skill_resource_tool
from tools import get_bom_tools

logger = logging.getLogger(__name__)

_JOULE = os.environ.get("JOULE_RUNTIME", "0") == "1"


class AgentExecutor(A2AAgentExecutor):
    def __init__(self):
        self.agent = SampleAgent()
        self.skill_tools = get_load_skill_resource_tool()
        # Pass the agent's S4 client so tools reuse the same singleton
        self.bom_tools = get_bom_tools(s4=self.agent.s4_client)

    async def _load_tools(self) -> list:
        if _JOULE:
            # Import MCP tools only in Joule runtime to avoid sap_cloud_sdk dep on CF
            try:
                from mcp_tools import get_mcp_tools, get_user_token  # type: ignore
                _user_tok = get_user_token()
                _ut = _user_tok
                mcp_tools = await get_mcp_tools(user_token=_ut)
                if not mcp_tools:
                    logger.warning("No tools returned from Agent Gateway")
                else:
                    logger.info("Loaded %d MCP tool(s)", len(mcp_tools))
                return mcp_tools
            except ValueError as e:
                logger.error("Invalid user token: %s", e)
            except Exception as e:
                logger.error("Failed to load MCP tools: %s", e)
            return []
        else:
            # CF runtime: BOM tools include direct S/4HANA calls — return them here too
            return [*self.bom_tools, *self.skill_tools]

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        query = context.get_user_input()
        task = context.current_task
        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        mcp_tools = await self._load_tools()
        # In CF runtime _load_tools() already includes bom_tools+skill_tools;
        # in Joule runtime we merge mcp_tools with bom_tools+skill_tools.
        if _JOULE:
            tools = [*mcp_tools, *self.bom_tools, *self.skill_tools]
        else:
            tools = mcp_tools  # already contains everything in CF mode
        if tools:
            logger.info("Total tools loaded: %d — %s", len(tools), [t.name for t in tools])

        updater = TaskUpdater(event_queue, task.id, task.context_id)

        try:
            async for item in self.agent.stream(query, task.context_id, tools=tools):
                is_task_complete = item["is_task_complete"]
                require_user_input = item["require_user_input"]
                content = item["content"]

                if require_user_input:
                    await updater.update_status(
                        TaskState.input_required,
                        new_agent_text_message(content, task.context_id, task.id),
                        final=True,
                    )
                    break
                elif is_task_complete:
                    await updater.add_artifact(
                        [Part(root=TextPart(text=content))], name="agent_result"
                    )
                    await updater.complete()
                    break
                else:
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(content, task.context_id, task.id),
                    )
        except Exception as e:
            logger.exception("Agent execution error")
            raise ServerError(error=InternalError()) from e

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())

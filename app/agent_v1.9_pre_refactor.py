"""
BOMA — agent.py AS-IS snapshot before v2.0 architectural refactor
==================================================================
Saved: pre-refactor v1.9 state
Reason: Full architectural redesign — planner-driven pipeline replacing
        intent-first routing. See v2.0 agent.py for new implementation.

TO REVERT:
  1. Delete agent.py
  2. Rename this file to agent.py
  3. cf push

KNOWN BUGS IN THIS VERSION (fixed in v2.0):
  BUG-01: Numeric material IDs like "2258" not extracted → falls back to session material
  BUG-02: "Main component responsible for price difference" → routes to BOM explosion (WRONG)
  BUG-03: Capability questions ("what can you do?") → blocked by domain guard
  BUG-04: "How much will I save?" → shows price table, not savings amount
  BUG-05: Sequential budget ("buy X then buy Y with remaining") → ignores first purchase
  BUG-06: BOM not found → confusing message instead of clean validation
  BUG-07: Stale session - previous material bleeds into next unrelated question
  BUG-08: Material name truncated: MZ-FG-M500 → MZ-FG
  BUG-09: Capability answer hardcoded to DEMOCAR examples
  BUG-10: Query understanding missing — nouns trigger tools, not business objective
"""
from __future__ import annotations

import logging
import os
import time
import urllib.request
import json as _json
import json
from dataclasses import dataclass
from typing import AsyncGenerator, Literal, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

logger = logging.getLogger(__name__)

_JOULE       = os.environ.get("JOULE_RUNTIME", "0") == "1"


def _sf(value, fmt=".2f") -> str:
    try:
        v = float(value) if value is not None else 0.0
        return format(v, fmt)
    except Exception:
        return "0"


def _sfc(value) -> str:
    try:
        v = float(value) if value is not None else 0.0
        return f"{v:,.0f}"
    except Exception:
        return "0"

_IBD_TESTING = os.environ.get("IBD_TESTING", "0") == "1"
THREAD_TTL_SECONDS = 3600

_fx_cache: dict = {}
_fx_cache_time: float = 0.0
_FX_TTL = 1800


def get_live_fx_rates() -> dict:
    global _fx_cache, _fx_cache_time
    now = time.monotonic()
    if _fx_cache and (now - _fx_cache_time) < _FX_TTL:
        return _fx_cache
    try:
        url = "https://open.er-api.com/v6/latest/USD"
        r = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(r, timeout=6) as resp:
            data = _json.loads(resp.read())
        rates = data.get("rates", {})
        if not rates:
            raise ValueError("empty rates")
        usd_inr = float(rates.get("INR", 96.35))
        usd_eur = float(rates.get("EUR", 0.873))
        usd_gbp = float(rates.get("GBP", 0.741))
        usd_jpy = float(rates.get("JPY", 149.5))
        usd_chf = float(rates.get("CHF", 0.893))
        _fx_cache = {
            "USD_INR": round(usd_inr, 4),
            "USD_EUR": round(usd_eur, 6),
            "USD_GBP": round(usd_gbp, 6),
            "USD_CHF": round(usd_chf, 6),
            "EUR_INR": round(usd_inr / usd_eur, 4),
            "EUR_USD": round(1.0 / usd_eur, 6),
            "GBP_INR": round(usd_inr / usd_gbp, 4),
            "GBP_USD": round(1.0 / usd_gbp, 6),
            "CHF_INR": round(usd_inr / usd_chf, 4),
            "CHF_USD": round(1.0 / usd_chf, 6),
            "JPY_INR": round(usd_inr / usd_jpy, 6),
            "INR_USD": round(1.0 / usd_inr, 8),
            "INR_EUR": round(usd_eur / usd_inr, 8),
            "INR_GBP": round(usd_gbp / usd_inr, 8),
            "_source": "open.er-api.com",
            "_as_of": data.get("time_last_update_utc", "live"),
        }
        _fx_cache_time = now
        logger.info("FX refreshed: 1 USD = %.4f INR", usd_inr)
    except Exception as exc:
        logger.warning("FX fetch failed: %s — using fallback", exc)
        if not _fx_cache:
            _fx_cache = {
                "USD_INR": 96.35, "USD_EUR": 0.873, "USD_GBP": 0.741, "USD_CHF": 0.893,
                "EUR_INR": 110.37, "EUR_USD": 1.145,
                "GBP_INR": 130.0, "GBP_USD": 1.35,
                "CHF_INR": 108.0, "CHF_USD": 1.12,
                "JPY_INR": 0.645,
                "INR_USD": 0.01038, "INR_EUR": 0.00906, "INR_GBP": 0.00769,
                "_source": "fallback", "_as_of": "offline",
            }
    return _fx_cache


# ── This is the v1.9 state of SampleAgent ────────────────────────────────────
# The SampleAgent class below is the EXACT code from agent.py as it existed
# before the v2.0 architectural refactor.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentResponse:
    status: Literal["input_required", "completed", "error"]
    message: str


class SampleAgent:
    """
    v1.9 SampleAgent — intent-first routing with Python formatters.
    Preserved here as fallback. Use agent.py (v2.0) for production.
    """
    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    # ── Session store: thread_id → {material, plants, bom, budget, ...} ──────
    _sessions: dict = {}

    def __init__(self, s4_client=None):
        self._llm: BaseChatModel | None = None
        self._checkpointer = InMemorySaver()
        self._last_active: dict[str, float] = {}
        try:
            from s4hana_client import Client as S4Client
            self._s4_client = s4_client if s4_client is not None else S4Client()
        except Exception:
            self._s4_client = s4_client

    @property
    def s4_client(self):
        return self._s4_client

    def _get_session(self, thread_id: str) -> dict:
        if thread_id not in self._sessions:
            self._sessions[thread_id] = {}
        return self._sessions[thread_id]

    def _touch(self, thread_id: str) -> None:
        now = time.monotonic()
        expired = [
            tid for tid, ts in list(self._last_active.items())
            if now - ts > THREAD_TTL_SECONDS
        ]
        for tid in expired:
            try:
                self._checkpointer.delete_thread(tid)
            except Exception:
                pass
            self._last_active.pop(tid, None)
            self._sessions.pop(tid, None)
        self._last_active[thread_id] = now

    async def _get_llm(self) -> BaseChatModel | None:
        if self._llm is not None:
            return self._llm
        if _IBD_TESTING:
            return None
        if _JOULE:
            try:
                from langchain_litellm import ChatLiteLLM
                self._llm = ChatLiteLLM(model="sap/anthropic--claude-4.5-sonnet", temperature=0.0)
            except Exception as e:
                logger.warning("Joule LLM unavailable: %s", e)
            return self._llm
        try:
            from aicore import init_llm_from_destination
            self._llm = await init_llm_from_destination(
                os.environ.get("AGENT_LLM_MODEL", "gpt-4o"), temperature=0.0
            )
        except Exception as e:
            logger.warning("AI Core LLM unavailable: %s", e)
            try:
                from aicore import build_aicore_llm
                self._llm = build_aicore_llm(
                    model=os.environ.get("AICORE_MODEL", "gpt-4o"), temperature=0.0
                )
            except Exception as e2:
                logger.warning("build_aicore_llm also failed: %s", e2)
        return self._llm

    # ── Intent detection ──────────────────────────────────────────────────────
    def _detect_intent(self, query: str, session: dict) -> dict:
        """
        v1.9 intent-first routing (REPLACED in v2.0 by planner-driven pipeline).
        Returns {intent, material, plants, bom_number, budget, currency}.
        """
        import re
        q = query.strip()
        ql = q.lower()

        # --- entity extraction ---
        # Material: uppercase tokens with optional hyphens/numbers
        mat_match = re.search(r'\b([A-Z][A-Z0-9\-]{2,20})\b', q)
        material = mat_match.group(1) if mat_match else session.get("material")

        # Plants: 4-digit numeric codes
        plants_found = list(dict.fromkeys(re.findall(r'\b(1\d{3}|2\d{3})\b', q)))

        # BOM number: 8-digit numeric
        bom_match = re.search(r'\b(0{4,}\d{3,8})\b', q)
        bom_number = bom_match.group(1) if bom_match else None

        # Budget
        budget_match = re.search(
            r'(\d+(?:\.\d+)?)\s*(cr|crore|lakh|lac|k|m|million|billion|bn)?\s*(usd|inr|eur|gbp)?',
            ql
        )
        budget = float(budget_match.group(1)) if budget_match else session.get("budget")
        budget_unit = (budget_match.group(2) or "").lower() if budget_match else ""
        budget_currency = (budget_match.group(3) or "usd").upper() if budget_match else session.get("budget_currency", "USD")

        if budget and budget_unit in ("cr", "crore"):
            budget = budget * 1e7
        elif budget and budget_unit in ("lakh", "lac"):
            budget = budget * 1e5
        elif budget and budget_unit == "m":
            budget = budget * 1e6
        elif budget and budget_unit == "bn":
            budget = budget * 1e9

        # --- intent classification ---
        if bom_number and any(k in ql for k in ["material for", "material against", "which material", "elaborate bom", "plant and material"]):
            intent = "reverse_bom"
        elif any(k in ql for k in ["bom number", "bill of material number", "stlnr"]):
            intent = "bom_numbers"
        elif any(k in ql for k in ["component", "explode", "bom structure", "what is in"]):
            intent = "bom_explosion"
        elif any(k in ql for k in ["save", "saving", "savings"]):
            intent = "savings"
        elif any(k in ql for k in ["how many", "how much", "can i buy", "afford", "budget", "purchase", "buy"]):
            intent = "budget"
        elif any(k in ql for k in ["price diff", "cost diff", "variance", "why diff", "reason", "responsible", "main component", "root cause", "driver"]):
            intent = "cost_variance"
        elif any(k in ql for k in ["price", "cost", "valuation", "compare", "cheaper", "expensive", "inr", "usd"]):
            intent = "price_comparison"
        elif bom_number:
            intent = "reverse_bom"
        else:
            intent = "general"

        return {
            "intent": intent,
            "material": material,
            "plants": plants_found if plants_found else session.get("plants", []),
            "bom_number": bom_number,
            "budget": budget,
            "budget_currency": budget_currency,
        }

    async def stream(
        self,
        query: str,
        context_id: str,
        tools: Sequence[BaseTool] | None = None,
    ) -> AsyncGenerator[dict, None]:
        self._touch(context_id)
        yield {"is_task_complete": False, "require_user_input": False, "content": "Processing..."}

        try:
            llm = await self._get_llm()
            if llm is None:
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": "LLM not configured."}
                return

            tool_list = list(tools) if tools else []
            graph = create_react_agent(
                llm, tools=tool_list,
                prompt=_get_v19_system_prompt(),
                checkpointer=self._checkpointer,
            )
            config = {"configurable": {"thread_id": context_id}}
            result = await graph.ainvoke(
                {"messages": [HumanMessage(content=query)]}, config
            )
            self._touch(context_id)
            response = result["messages"][-1].content
            yield {"is_task_complete": True, "require_user_input": False, "content": response}

        except Exception as e:
            logger.exception("Agent stream() failed")
            yield {"is_task_complete": True, "require_user_input": False,
                   "content": f"Error: {str(e)}"}

    async def invoke(
        self,
        query: str,
        context_id: str,
        tools: Sequence[BaseTool] | None = None,
    ) -> AgentResponse:
        last: dict = {}
        async for chunk in self.stream(query, context_id, tools=tools):
            last = chunk
        if last.get("is_task_complete"):
            return AgentResponse(status="completed", message=last["content"])
        if last.get("require_user_input"):
            return AgentResponse(status="input_required", message=last["content"])
        return AgentResponse(status="error", message=last.get("content", "Unknown error"))


def _get_v19_system_prompt() -> str:
    """v1.9 system prompt — preserved for rollback reference."""
    return """You are BOMA — BOM & Cost Analyst connected live to SAP CIN_100.
Call tools to fetch real data. Never fabricate SAP data."""

"""
BOMA v2.0 — agent.py — Planner-Driven SAP Business Reasoning Agent
====================================================================

ARCHITECTURE (replaces v1.9 intent-first routing):

  User Query
    │
    ▼
  [1] Entity Extractor          — extract material, plant, BOM#, budget (EXACT, no truncation)
    │                              current query ALWAYS wins over session
    ▼
  [2] Query Understander        — classify BUSINESS OBJECTIVE (not keywords)
    │                              "main component responsible" → COST_VARIANCE_ANALYSIS
    │                              "what can you do" → CAPABILITY_QUESTION
    ▼
  [3] Domain Validator          — out-of-domain, greet, capability handled HERE (no SAP tools)
    │
    ▼
  [4] Planner                   — build deterministic execution graph
    │                              objective → ordered tool calls + calculations
    ▼
  [5] Orchestrator (LLM ReAct)  — execute plan with SAP tools, preserve step results
    │
    ▼
  [6] Deterministic Calculator  — savings, budget units, FX (arithmetic, not LLM)
    │
    ▼
  [7] Response Validator        — did the answer match the business objective?
    │                              if NO → retry with corrective prompt
    ▼
  [8] Response Generator        — format with business context
    │
    ▼
  Response
"""

from __future__ import annotations

import json as _json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import AsyncGenerator, Literal, Optional, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from entity_extractor import extract_entities
from query_understander import understand_query, QueryObjective
from planner import build_plan, ExecutionPlan
from session_manager import get_session_manager
from calculator import (
    calculate_savings, calculate_budget_units,
    calculate_multi_step_budget, calculate_assembly_cost,
    format_inr, get_fx_rate,
)
from response_validator import validate_response, strip_banned_phrases

logger = logging.getLogger(__name__)

_JOULE       = os.environ.get("JOULE_RUNTIME", "0") == "1"
_IBD_TESTING = os.environ.get("IBD_TESTING", "0") == "1"


# ---------------------------------------------------------------------------
# FX rate cache (shared across module)
# ---------------------------------------------------------------------------
_fx_cache: dict = {}
_fx_cache_time: float = 0.0
_FX_TTL = 1800   # 30 minutes


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
        usd_inr = float(rates.get("INR", 95.69))
        usd_eur = float(rates.get("EUR", 0.873))
        usd_gbp = float(rates.get("GBP", 0.741))
        usd_chf = float(rates.get("CHF", 0.893))
        usd_jpy = float(rates.get("JPY", 149.5))
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
                "USD_INR": 95.69, "USD_EUR": 0.873, "USD_GBP": 0.741, "USD_CHF": 0.893,
                "EUR_INR": 109.6, "EUR_USD": 1.145,
                "GBP_INR": 129.2, "GBP_USD": 1.35,
                "CHF_INR": 107.1, "CHF_USD": 1.12,
                "JPY_INR": 0.641,
                "INR_USD": 0.01045, "INR_EUR": 0.00912, "INR_GBP": 0.00774,
                "_source": "fallback", "_as_of": "offline",
            }
    return _fx_cache


# ---------------------------------------------------------------------------
# BOMA Capability Registry — dynamic, not hardcoded to DEMOCAR
# ---------------------------------------------------------------------------
_BOMA_CAPABILITIES = """
🤖 **I'm BOMA — SAP BOM & Cost Analyst**, connected live to **SAP CIN_100**.

Here's what I can do for **any material, any plant**:

| Capability | Example Question |
|---|---|
| 📋 BOM Numbers | "What are BOM numbers for DEMOCAR across all plants?" |
| 🔁 Reverse BOM | "What material and plant belong to BOM 00000052?" |
| 🌿 BOM Components | "Show BOM components of F-10A at plant 1010" |
| 💰 Price Lookup | "What is the price of DEMOCAR at plant 1710?" |
| 📊 Price Comparison | "Compare price of DEMOCAR at plants 1710 and 1720" |
| 💡 Savings Calculation | "How much will I save buying DEMOCAR from plant 1710 vs 1720?" |
| 🔬 Cost Variance Drill-Down | "Why does DEMOCAR cost more at plant 1720 than 1710?" |
| 🏭 Make vs Buy | "Should SEAT be made in-house or bought externally?" |
| 📈 Cost Optimisation | "What are cost optimisation opportunities for DEMOCAR?" |
| 💼 Budget Analysis | "With 2 CR USD, how many DEMOCAR can I buy from plant 1710?" |
| 🔢 Multi-Step Budget | "Buy DEMOCAR, then use remaining budget to buy F-10A" |
| 🔄 Currency Conversion | "What is 65000 USD in INR?" |
| 🏭 Plant Discovery | "Which plants have DEMOCAR?" |
| 🤝 Procurement Strategy | "What is the procurement strategy for SEATCOVER?" |

Just ask me anything about **any SAP material** — I fetch **live data from CIN_100** every time.
"""

_GREETING_RESPONSE = """
👋 Hello! I'm **BOMA** — your Agentic AI BOM & Cost Analyst.

I'm connected live to **SAP CIN_100** and work completely independently — ask me about **any material, any plant**.

Try asking:
- *"What are the BOM numbers for DEMOCAR?"*
- *"Compare price of F-10A at plants 1010 and 1710"*
- *"Why does DEMOCAR cost more at plant 1720?"*
- *"With 2 CR USD, how many DEMOCAR can I buy from plant 1710?"*
"""

_OUT_OF_DOMAIN_RESPONSE = """
👋 I'm **BOMA** — your SAP BOM & Cost Analyst, connected live to CIN_100.

I specialise in **SAP Materials Management (MM)**, Bills of Material (BOM), procurement analysis, and cost optimization.

I can't help with that question, but I'm an expert at SAP topics like:
- BOM numbers and structures
- Cross-plant price comparison
- Cost variance root cause analysis
- Budget and affordability analysis
- Make vs Buy recommendations

Ask me anything about SAP materials in CIN_100!
"""


# ---------------------------------------------------------------------------
# STABLE base system prompt — passed to create_react_agent (fixed, never changes)
# All per-request planning context is carried in the orchestration_msg instead.
# ---------------------------------------------------------------------------
_BOMA_BASE_SYSTEM_PROMPT = """You are BOMA — an SAP Business Reasoning Agent connected live to SAP CIN_100.
You think like an experienced SAP MM Functional Consultant.
You fetch real data from SAP using your tools. You NEVER fabricate, estimate, or guess SAP data.

REASONING PROCESS (follow this every time):
1. UNDERSTAND: What does the user ACTUALLY want? (not just what they typed)
2. PLAN: Which SAP tools do I need to call, in what order?
3. EXECUTE: Call tools one by one, preserve all results.
4. REASON: What do the results tell me? Calculate, compare, rank.
5. VALIDATE: Does my answer actually satisfy the user's question?
6. RESPOND: Present the PRIMARY answer first, supporting data second.

CORE RULES:
1. Always call SAP tools to fetch real data before answering.
2. Process ALL materials mentioned — never stop after the first one.
3. If a material or BOM is not found: "Material X does not exist in SAP CIN_100."
4. Always show amounts in both original currency AND INR equivalent.
5. Never say "click GO" or ask the user to run any UI action.
6. For cost variance/WHY questions → use explain_bom_cost_variance, NOT get_bom_explosion.
7. For savings questions → calculate the savings amount; price table alone is NOT enough.
8. For follow-ups ("what about others", "remaining") → process the PENDING materials from session context.
9. For complete hierarchy requests → traverse ALL BOM levels, not just Level 1.
10. For new material planning → use provided component prices; do NOT say "material not found".
11. For cross-plant sourcing optimization across MULTIPLE components (a BOM, or a hypothetical
    assembly the user is planning) → evaluate EACH component INDEPENDENTLY and recommend the
    cheapest plant for THAT specific component. Different components MAY come from different
    plants — this "mixed sourcing" is normal and often the most cost-effective outcome. Do NOT
    collapse the answer into a single "globally cheapest plant for everything" unless every
    component happens to be cheapest at the same plant, or the user explicitly asks to
    single-source from one plant/supplier.
12. Always explain WHY you recommend a plant: cost difference, savings amount, major drivers.
13. NEVER double-count BOM costs. In a multi-level BOM, a parent assembly's own standalone price
    already includes the value of everything below it (SAP standard/cumulative costing). When
    computing a "Total Cost" for the whole assembly, sum ONLY the leaf-level components (or the
    tool's totalCost field, which already does this) — never add a parent's price on top of its
    own children's prices, and never add costs from more than one level of the same branch.
14. PRICE UNIT RULE — CRITICAL: The tool result field "effectivePrice" is ALREADY normalised
    to price-per-1-unit. The field "priceUnit" is always 1 in the normalised result.
    NEVER multiply effectivePrice × priceUnit. Use effectivePrice directly for INR conversion:
      INR = effectivePrice × FX_rate   (NOT effectivePrice × priceUnit × FX_rate)
15. FLOOR DIVISION RULE: NEVER do floor division or large-number subtraction yourself.
    ALWAYS call run_cost_calculator — it does the exact Python arithmetic and returns
    units_affordable, total_spent_inr, remaining_inr with full precision.
    • For BUDGET ANALYSIS: pass components, budget, budget_currency → read units_affordable
    • For HYPOTHETICAL PLANNING: pass all components + extra_charges, budget → read all fields
    Commas in ₹ numbers are thousands separators, NOT decimals — the calculator handles this.
16. BUDGET CURRENCY RULE: NEVER pre-convert budget to INR yourself before calling run_cost_calculator.
    Pass the RAW budget number and its original currency. The calculator converts internally.
    WRONG: budget=1767273246721, budget_currency="INR"  (pre-converted — FORBIDDEN)
    CORRECT: budget=15000000, budget_currency="EUR"      (raw — let calculator convert)
    Do NOT call get_tcurr_fx_rate and then multiply budget × rate yourself.
    Read budget_inr FROM the calculator result — never compute it yourself.

TOOL SELECTION GUIDE:
- BOM number lookup → get_bom_headers
- Reverse BOM (BOM# → material) → get_material_by_bom_number
- BOM components → get_bom_explosion
- Full hierarchy → deep_bom_cost_analysis (max_depth=6)
- WHY cost differs → explain_bom_cost_variance (recursive, NOT bom_explosion)
- Cheapest plant + price → get_cheapest_material_valuation (auto-discovers all plants)
- ALL arithmetic (units, remaining) → run_cost_calculator (never compute yourself)
- Procurement strategy → get_material_procurement_type + get_purchase_info_records
- Cross-plant optimisation → list_material_plants + deep_bom_cost_analysis

RESPONSE DEPTH REQUIREMENTS — every answer must do all three:
1. DIRECT ANSWER: State the fact/number immediately (price, units, BOM number, etc.)
2. BUSINESS CONTEXT: Explain what it means:
   - Price result → is it Standard (S) or Moving Average (V)? What does the price control mean for procurement?
   - Budget result → what % of budget is consumed? Is the remaining budget enough for any partial purchase?
   - BOM result → what materials/plants are involved? Any notable observations (single plant, multi-plant)?
   - Cost variance → which component/sub-assembly is the BIGGEST driver? What % of total variance is it?
   - Cheapest plant → by how much is it cheaper than the next-cheapest? What is the savings opportunity?
3. ACTIONABLE RECOMMENDATION: End every answer with 1-2 concrete next steps or business insights:
   - "Recommend: source from plant 1010 to save ₹8,43,188 per unit vs plant 1710"
   - "Consider: F-10B at plant 1710 uses Moving Average pricing — volatility risk in cost planning"
   - "Opportunity: switching DEMOBODY to external procurement (like plant 1710) could reduce cost by 16.7%"
   Never give generic filler. Make recommendations specific to the actual data returned.

FORMAT RULES:
- Use a data table for comparisons (price, BOM, components)
- Bold the PRIMARY answer number/conclusion
- Use bullet points for recommendations
- Show both original currency AND ₹ (INR) for all monetary values

SAP SYSTEM: CIN_100 | Connected via BTP Destination Service"""


# ---------------------------------------------------------------------------
# v2.0 System Prompt — Planner-Aware (used in orchestration message, not graph)
# ---------------------------------------------------------------------------
def _build_system_prompt(plan: ExecutionPlan, entities: dict, fx_rates: dict) -> str:
    usd_inr = fx_rates.get("USD_INR", 95.69)
    eur_inr = fx_rates.get("EUR_INR", 109.6)

    material = entities.get("material", "")
    plants = entities.get("plants", [])
    bom_number = entities.get("bom_number", "")
    budget = entities.get("budget")
    budget_currency = entities.get("budget_currency", "USD")

    plan_description = f"""
EXECUTION PLAN FOR THIS REQUEST:
  Objective: {plan.objective}
  Primary Output: {plan.primary_output}
  Steps: {", ".join(f"[{s.step}] {s.action}:{s.tool or s.calc_type or s.format}" for s in plan.steps)}

ENTITIES EXTRACTED FROM CURRENT QUERY:
  Material: {material or "not specified"}
  Plants: {plants or "not specified"}
  BOM Number: {bom_number or "not specified"}
  Budget: {f"{budget_currency} {budget:,.0f}" if budget else "not specified"}

CRITICAL EXECUTION RULES:
1. Follow the execution plan above — call tools in the stated order.
2. NEVER skip steps. If step 1 gets a plant list, use those plants in step 2.
3. NEVER reuse data from previous conversation turns for tool args.
   Always use material="{material}", plants={plants} from THIS query.
4. If a material is not found in SAP, say exactly:
   "Material {material} does not exist in SAP CIN_100. Please verify the material number."
   DO NOT search for or return data for a different material.
5. If a BOM number is not found, say:
   "BOM {bom_number} does not exist in SAP CIN_100. Please verify the BOM number."

PRIMARY OUTPUT REQUIREMENT:
  Your response must deliver: {plan.primary_output}
  This is what the user asked for. All other data is SUPPORTING context only.
"""

    objective_instructions = _get_objective_instructions(plan.objective, entities, usd_inr)

    return f"""You are BOMA — an SAP Business Reasoning Agent connected live to SAP CIN_100.
You think like an experienced SAP MM Functional Consultant.
You fetch real data from SAP. You never fabricate, estimate, or guess SAP data.

{plan_description}

OBJECTIVE-SPECIFIC INSTRUCTIONS:
{objective_instructions}

FX RATES (from live SAP TCURR + open.er-api.com):
  1 USD = ₹{usd_inr:.4f}
  1 EUR = ₹{eur_inr:.4f}

FORMATTING RULES:
- Use ₹ symbol for all INR amounts
- Use Indian number format: ₹6,22,01,300 or say "₹62.2 Crore"
- Always show original currency AND INR equivalent
- Use markdown tables for multi-row data
- Show plant codes + SAP transaction codes at the end (e.g. 📋 CS03 · MM03)

SELF-CHECK BEFORE RESPONDING:
1. Did I answer what the user actually asked? (not a related question)
2. Did I use material={material} and plants={plants} from this query?
3. Is the PRIMARY output ({plan.primary_output}) the dominant part of my response?
4. Did I call all required SAP tools from the execution plan?
5. Are all amounts shown in both original currency AND INR?
"""


def _get_objective_instructions(objective: str, entities: dict, usd_inr: float) -> str:
    material = entities.get("material", "")
    plants = entities.get("plants", [])
    budget = entities.get("budget")
    budget_currency = entities.get("budget_currency", "USD")

    if objective == QueryObjective.PRICE_LOOKUP:
        plant = plants[0] if plants else ""
        return f"""
PRICE LOOKUP PROCEDURE:
1. Call get_material_valuation(material_number="{material}"{f', plant="{plant}"' if plant else ''})

YOUR RESPONSE MUST SHOW:
  ✅ **Price: <amount> <currency>** (this is the PRIMARY answer — make it prominent)
  ✅ Price in INR equivalent (using live FX rate)
  ✅ Price Control type and what it means:
     - Standard Price (S): Fixed cost for the accounting period. Good for stable cost planning.
       Any goods receipt/issue uses this price — no price fluctuation risk.
     - Moving Average Price (V): Recalculated on every goods receipt. Reflects actual market cost.
       Risk: price can drift from plan if supplier prices change.
  ✅ SAP valuation class or costing context if available
  ✅ Business insight: Is this price high/low compared to typical range? (use rankedPlants if available)
  ✅ Recommendation: if this material exists at other plants, mention that a price comparison is possible.
"""

    if objective == QueryObjective.SAVINGS_CALCULATION:
        p1 = plants[0] if len(plants) > 0 else "plant1"
        p2 = plants[1] if len(plants) > 1 else "plant2"
        return f"""
SAVINGS CALCULATION PROCEDURE:
1. Call get_material_valuation for {material} at {p1} → get price1, currency1
2. Call get_material_valuation for {material} at {p2} → get price2, currency2
3. Convert BOTH prices to INR using the FX rates above
4. Calculate: savings_inr = |price1_inr - price2_inr|
5. Calculate: savings_pct = savings_inr / max(price1_inr, price2_inr) × 100

YOUR RESPONSE MUST SHOW:
  ✅ SAVINGS AMOUNT IN INR (this is the PRIMARY answer)
  ✅ Savings percentage
  ✅ Which plant is cheaper (recommendation)
  ✅ Price table (supporting context only — NOT the main answer)

FORBIDDEN: Do not show only a price comparison table as the answer.
"""

    if objective == QueryObjective.COST_VARIANCE_ANALYSIS:
        return f"""
COST VARIANCE ANALYSIS PROCEDURE:
1. Call explain_bom_cost_variance(material_number="{material}", plant1="{plants[0] if plants else ''}", plant2="{plants[1] if len(plants)>1 else ''}")
   This tool recursively drills through the entire BOM tree to find WHY cost differs.
2. From the result, extract: topCostDrivers, recommendations, flatExplanation

YOUR RESPONSE MUST SHOW:
  ✅ HEADLINE: one sentence — total variance %, cheaper plant, absolute savings in INR
  ✅ ROOT CAUSE SECTION: 3–5 top components ranked by their contribution to the price gap
     For each driver show:
       - Component code + description
       - Price at each plant (native currency + INR)
       - Contribution to total gap (₹ and % of total variance)
       - Procurement type at each plant (E=External / F=In-house)
       - Root cause label: PROCUREMENT_TYPE_DIFF | SUPPLIER_PRICE_DIFF | OVERHEAD | VOLUME
  ✅ SECOND-ORDER ANALYSIS: If a component is in-house at one plant and external at the other,
     explain the implication:
       - In-house usually = overhead-loaded cost; External = market-driven
       - Recommend: SWITCH to external procurement for that component (+ estimated saving)
       - Or: CONSOLIDATE — source in-house component from the plant where it's external
  ✅ RECOMMENDATIONS TABLE:
     | # | Component | Action | Expected Saving | Priority |
     Actions = RENEGOTIATE_SUPPLIER | SWITCH_TO_EXTERNAL | CONSOLIDATE_SOURCING | REVIEW_OVERHEAD
  ✅ TOTAL ACHIEVABLE SAVING: sum of all recommendation savings in ₹ and as % of expensive plant cost

ADVANCED INTELLIGENCE RULES:
  • Never just say "procurement type difference" — say WHO pays more and WHY it costs more
  • If the cheaper plant buys externally and the expensive one makes in-house, the fix is to
    source externally: quantify the saving before recommending
  • If variance < 5% on a component, label it "Immaterial" and skip detailed analysis
  • Group components by cause to reduce noise: don't list 8 variants of the same root cause
  • Lead with the highest-impact item — the user wants to know what to fix first

FORBIDDEN: Do NOT just list BOM components. The user asked WHY — answer WHY with numbers.
"Main component responsible" means: identify the BIGGEST contributor, state the CAUSE,
quantify the saving if fixed.
"""

    if objective == QueryObjective.BUDGET_ANALYSIS:
        budget_str = f"{budget_currency} {budget:,.0f}" if budget else "given budget"
        if plants:
            # User pinned a specific plant — respect that, no discovery needed.
            plant = plants[0]
            return f"""
BUDGET ANALYSIS PROCEDURE (plant explicitly specified: {plant}):
1. Call get_material_valuation(material_number="{material}", plant="{plant}") → get unit_price, currency
2. Call run_cost_calculator(
     components=[{{"name": "{material}", "qty": 1, "price": <unit_price>, "currency": <currency>, "plant": "{plant}"}}],
     budget={budget or 0}, budget_currency="{budget_currency}"
   )
3. Read: units_affordable, total_spent_inr, remaining_inr, budget_inr from the calculator result.
   DO NOT compute these yourself.

YOUR RESPONSE MUST SHOW:
  ✅ Number of units affordable (this is the PRIMARY answer) — from calculator.units_affordable
  ✅ Total cost spent in INR — from calculator.total_spent_inr
  ✅ Remaining budget in INR — from calculator.remaining_inr (NEVER show ₹0 unless truly 0)
  ✅ Unit price in original currency and INR
  ✅ Budget utilisation %: (total_spent_inr / budget_inr) × 100
  ✅ What the remaining budget could buy (e.g., "Remaining ₹X ≈ Y partial units; not enough for 1 more complete unit")
  ✅ Recommendation: is this a good use of the budget? Any cost optimisation opportunity?
"""
        # No plant specified — use get_cheapest_material_valuation to find best plant
        return f"""
BUDGET ANALYSIS PROCEDURE (NO plant specified — find the cheapest automatically):
1. Call get_cheapest_material_valuation(material_number="{material}")
   This returns: cheapestPlant, cheapestPriceINR (already in INR), rankedPlants.
2. Call run_cost_calculator(
     components=[{{"name": "{material}", "qty": 1, "price": <cheapestPriceINR>, "currency": "INR", "plant": <cheapestPlant>}}],
     budget={budget or 0}, budget_currency="{budget_currency}"
   )
3. Read: units_affordable, total_spent_inr, remaining_inr, budget_inr from the calculator result.
   DO NOT compute these yourself — trust the calculator exactly.

YOUR RESPONSE MUST SHOW:
  ✅ Which plant you picked and WHY (its INR price vs every other plant from rankedPlants)
  ✅ units_affordable (from calculator) — PRIMARY answer
  ✅ total_spent_inr (from calculator)
  ✅ remaining_inr (from calculator) — NEVER show ₹0 unless the calculator says 0
  ✅ Budget utilisation %: (total_spent_inr / budget_inr) × 100
  ✅ Ranked plant table: show ALL plants with their prices, highlighting which was chosen and why
  ✅ Savings vs most expensive plant: how much cheaper is the cheapest option?
  ✅ Recommendation: is there a meaningful difference between plants? Procurement advice.

FORBIDDEN: Do not call get_material_valuation for individual plants — get_cheapest_material_valuation
already did this. Do NOT do floor division or subtraction yourself — use run_cost_calculator.
"""

    if objective == QueryObjective.MULTI_STEP_BUDGET:
        return f"""
MULTI-STEP BUDGET PROCEDURE:
Each material gets its OWN cheapest-plant search — do NOT force every material
to come from the same plant unless the user explicitly asked to source
everything from one supplier/plant.

For EACH material mentioned (material1, material2, ... in the order given):
1. Call get_cheapest_material_valuation(material_number=materialN)
   Returns: cheapestPlant, cheapestPriceINR (already in INR), rankedPlants.
2. After collecting all prices, call run_cost_calculator once per step:
   • Step A: run_cost_calculator(
       components=[{{"name": mat1, "qty": 1, "price": cheapestPriceINR_mat1, "currency": "INR", "plant": cheapestPlant_mat1}}],
       budget={budget or 0}, budget_currency="{budget_currency}"
     ) → read units_affordable_mat1, remaining_inr_A
   • Step B: run_cost_calculator(
       components=[{{"name": mat2, "qty": 1, "price": cheapestPriceINR_mat2, "currency": "INR", "plant": cheapestPlant_mat2}}],
       budget=remaining_inr_A, budget_currency="INR"
     ) → read units_affordable_mat2, remaining_inr_B

READ calculator outputs — do NOT compute these yourself.

YOUR RESPONSE MUST SHOW, for EACH material:
  ✅ Which plant was cheapest for THAT material and its INR price vs the other plants
  ✅ units_affordable + total_spent_inr + remaining_inr (all from calculator)
  ✅ Final summary: total units of each material + remaining budget

FORBIDDEN: Do not source every material from the same plant "for simplicity" —
each material is priced and evaluated independently. Never do floor division yourself.
"""

    if objective == QueryObjective.REVERSE_BOM_LOOKUP:
        bom = entities.get("bom_number", "")
        return f"""
REVERSE BOM LOOKUP:
1. Call get_material_by_bom_number(bom_number="{bom}")
2. If result contains material and plant → show them clearly
3. If NOT FOUND → say "BOM {bom} does not exist in SAP CIN_100. Please verify."
   Do NOT return data for a different BOM number.

YOUR RESPONSE MUST SHOW:
  ✅ BOM Number | Material | Plant | Category | Status — in a clear table
  ✅ What this BOM is used for (Category M = Manufacturing/Production BOM, E = Equipment, etc.)
  ✅ Business context: e.g. "BOM {bom} is a Manufacturing BOM for material X at plant Y — used in production orders and standard cost calculation."
  ✅ What the user can do next: "You can explode this BOM to see components, or check the price of this material at its plant."
"""

    if objective == QueryObjective.BOM_NUMBER_LOOKUP:
        plant_str = f"at plant {plants[0]}" if len(plants) == 1 else "across all plants"
        return f"""
BOM NUMBER LOOKUP:
1. Call get_bom_headers(material_number="{material}"{f', plant="{plants[0]}"' if len(plants)==1 else ''})
2. Show BOM numbers found in a table

YOUR RESPONSE MUST SHOW:
  ✅ Table: Plant | BOM Number | Category | Variant | Usage | Status
  ✅ Total count of BOMs found
  ✅ Brief context: e.g. "This material has BOMs at {len(plants) or 'N'} plants — multi-plant manufacturing indicates separate standard cost runs per plant."
  ✅ What the BOM category (M = Manufacturing, E = Equipment, etc.) means for this material
  ✅ SAP verification: CS03 → Material: {material}
  ✅ Recommendation: which plant's BOM to inspect first and why (if multiple)
"""

    if objective == QueryObjective.PRICE_COMPARISON:
        return f"""
PRICE COMPARISON:
1. Get valuation for {material} at each plant in {plants}
2. Get FX rate USD→INR
3. Rank plants from cheapest to most expensive

YOUR RESPONSE MUST SHOW:
  ✅ Ranked table: Plant | Price | Currency | Price (INR) | Price Control
  ✅ **Cheapest plant highlighted** with its INR price
  ✅ Variance % and INR difference between cheapest and most expensive
  ✅ FX rate source
  ✅ Business context: What does the price control type (S=Standard, V=Moving Average) mean?
     Standard Price = fixed for period, good for cost stability; Moving Average = changes with GR, reflects market price.
  ✅ Procurement insight: Why might the same material cost different amounts at different plants?
     (Different procurement types, different suppliers, different overhead rates, different BOM depths)
  ✅ Recommendation: which plant to source from and the exact INR saving per unit vs the most expensive plant
"""

    if objective == QueryObjective.HYPOTHETICAL_PLANNING:
        budget_str = f"{budget_currency} {budget:,.0f}" if budget else "given budget"
        return f"""
HYPOTHETICAL ASSEMBLY PLANNING PROCEDURE
(The new material doesn't exist in SAP yet — do NOT search for it or say "not found".)
Instead: fetch each COMPONENT's price from SAP, then call run_cost_calculator to get
exact assembly cost and units affordable — NO manual arithmetic required.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1 — For EACH component independently:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Call get_cheapest_material_valuation(material_number=COMPONENT)
  From each result, record:
    cheapestPlant       → plant to source from
    cheapestPriceINR    → price per unit IN INR (already converted — DO NOT multiply by FX again)
    cheapestEffectivePrice + cheapestCurrency → for display only

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 2 — Run the deterministic calculator:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Call run_cost_calculator with:
    components = [
      {{"name": "COMP1", "qty": <qty_from_query>, "price": <cheapestPriceINR>, "currency": "INR", "plant": "<cheapestPlant>"}},
      ... (one entry per component)
    ]
    extra_charges = [  ← include accessories / labor from the query, or []
      {{"name": "Accessories", "amount": <amount>, "currency": "<CCY>"}},
    ]
    budget = {budget or 0}          ← raw number, e.g. 1000000 for 1M USD
    budget_currency = "{budget_currency}"

  CRITICAL RULES for building the components list:
    • price  = cheapestPriceINR (it is ALREADY in INR — set currency="INR")
    • qty    = number of units of this component in ONE assembled unit
    • DO NOT multiply price by FX again — it's already INR
    • budget = raw integer (1000000, NOT "1M" or "1,000,000")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 3 — Read results from run_cost_calculator:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  total_cost_per_unit_inr  → cost to build ONE assembly (all components + charges)
  units_affordable         → how many complete assemblies can be built with the budget
  total_spent_inr          → budget consumed
  remaining_inr            → exact remaining budget (use this number directly)
  budget_inr               → budget converted to INR

  DO NOT recompute these yourself — trust the calculator output exactly.

YOUR RESPONSE MUST SHOW:
  ✅ Component table: Material | Qty | Cheapest Plant | Price (native) | Price (INR) | Line Total (INR)
  ✅ Extra charges (if any)
  ✅ Total cost per assembly unit (from run_cost_calculator.total_cost_per_unit_inr)
  ✅ Budget: {budget_str} → ₹(budget_inr from calculator)
  ✅ Units = units_affordable (from calculator)   ← PRIMARY ANSWER
  ✅ Total spent = total_spent_inr (from calculator)
  ✅ Remaining = remaining_inr (from calculator)  ← show this EXACTLY, do NOT re-subtract

FORBIDDEN:
  • Do NOT say the new material "not found" — it is planned, not yet created
  • Do NOT call get_material_valuation for individual plants — use get_cheapest_material_valuation
  • Do NOT multiply cheapestPriceINR by any FX rate (already in INR)
  • Do NOT do floor division yourself — read units_affordable from run_cost_calculator
  • Do NOT subtract budget_inr - total_spent_inr yourself — read remaining_inr from run_cost_calculator
  • Do NOT round REMAINING to ₹0 — the calculator always gives the precise remainder
  • CRITICAL — Do NOT call get_tcurr_fx_rate before run_cost_calculator. The calculator handles ALL FX internally.
  • CRITICAL — Do NOT pre-convert budget to INR yourself. NEVER compute budget_INR = budget × any_rate.
    Pass the RAW budget number (e.g., 15000000 for 15M EUR) and budget_currency="EUR" to run_cost_calculator.
    Read budget_inr from the calculator result — do NOT compute it yourself.
    WRONG: budget=1767273246721, budget_currency="INR"  ← this is pre-converted, FORBIDDEN
    CORRECT: budget=15000000, budget_currency="EUR"     ← raw number, let calculator convert

PROFIT / COMPARISON ANALYSIS (when user asks "will I be in profit?" or "is it cheaper to buy?"):
  If the user compares self-assembly vs buying ready-made from a specific plant:
  1. self_assembly_cost = total_cost_per_unit_inr from run_cost_calculator
  2. buy_from_plant_cost = cheapestPriceINR from get_cheapest_material_valuation (for the final product)
  3. profit_per_unit = buy_from_plant_cost − self_assembly_cost
     → if profit_per_unit > 0: "Yes, you are in profit: ₹X per unit (Y%)"
     → if profit_per_unit < 0: "No, self-assembly costs more by ₹|X| per unit"
  Show a clear comparison table: Self-assembly cost vs Buy-from-plant cost vs Profit/Loss.

SINGLE-PLANT ASSEMBLY (when user says "assemble all from plant X" or "buy all from plant 1710"):
  If a specific plant is mentioned for ALL components (not cheapest-auto-discover):
  1. For EACH component: call get_material_valuation(material_number=COMP, plant=X)
  2. If a component is NOT found at plant X: SKIP it (exclude from cost), note it as "not available at plant X"
     DO NOT set its price = 0. DO NOT include ₹0 components in run_cost_calculator.
  3. Build components list ONLY from components that have valid prices at plant X.
  4. Call run_cost_calculator with only the FOUND components (skip missing ones entirely).
  5. In the response: clearly list which components were found vs skipped.
  6. If ZERO components found at plant X: state "None of the components have pricing at plant X" — do NOT return ₹0.
  COMPARISON with cheapest sourcing: show the cost delta (₹X more/less per unit vs cheapest sourcing).
"""


    if objective in (
        QueryObjective.CROSS_PLANT_OPTIMISATION,
        QueryObjective.MAKE_VS_BUY,
        QueryObjective.COST_OPTIMISATION,
    ):
        return f"""
MULTI-COMPONENT PLANT SOURCING PROCEDURE (mixed sourcing is expected):
Each component/material gets its OWN cheapest-plant decision — do NOT force every
component to come from the same plant "for simplicity" unless the user explicitly
asked to single-source from one plant/supplier.

For EACH component or material involved:
1. Call list_material_plants (or use the plants already given) → every plant it's available at.
2. Call get_material_valuation at EVERY one of those plants.
3. Convert ALL prices to the SAME currency (INR) before comparing — never compare
   raw USD/EUR/INR figures directly.
4. Pick the plant with the LOWEST INR price for THAT component specifically.
5. Repeat independently for every other component — its cheapest plant can be
   (and often is) different from the others.

If this is a BOM cost rollup (use deep_bom_cost_analysis): read the per-component
cheaperPlant from crossPlantVariance for EACH material — do not just report
summary.cheapestPlant, which only tells you the cheapest SINGLE plant if you bought
the entire assembly from one place. Also do not double-count: a parent assembly's
price already includes its children's value, so only sum leaf-level costs
(totalCost / includedInTotal already does this for you).

YOUR RESPONSE MUST SHOW:
  ✅ A table: Component | Plant chosen | Price (native + INR) | Price at other plant(s)
  ✅ Per-component reasoning for WHY that plant was picked
  ✅ Final optimized total cost = sum of each component's CHOSEN cheapest-plant price
     (never the price from forcing everything onto one plant, unless that's genuinely cheapest)

FORBIDDEN: Do not default every component to the same plant unless you've verified it is
genuinely the cheapest for each one individually. If the user pushes back saying a specific
component is cheaper elsewhere, RE-CHECK that component's price at every plant and correct
your answer with numbers — don't just apologize without recalculating.
"""

    return ""  # No specific instruction for other objectives


@dataclass
class AgentResponse:
    status: Literal["input_required", "completed", "error"]
    message: str
    tokens_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    time_taken_ms: int = 0


class SampleAgent:
    """
    BOMA v2.0 — Planner-driven SAP Business Reasoning Agent.

    Pipeline:
      extract_entities → understand_query → build_plan → orchestrate → validate → respond
    """
    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self, s4_client=None):
        self._llm: Optional[BaseChatModel] = None
        self._graph = None          # cached graph instance (v6 pattern)
        self._checkpointer = InMemorySaver()
        self._session_mgr = get_session_manager()
        # Mutable dict shared with the cached _pre_model_hook closure.
        # Updated before every ainvoke so the hook always has fresh system prompt
        # and the correct prior-message-count for trimming stale context.
        self._hook_state: dict = {"system_prompt": _BOMA_BASE_SYSTEM_PROMPT, "prior_msg_count": 0}
        try:
            from s4hana_client import Client as S4Client
            self._s4_client = s4_client if s4_client is not None else S4Client()
        except Exception:
            self._s4_client = s4_client

    @property
    def s4_client(self):
        return self._s4_client

    def _get_session(self, thread_id: str) -> dict:
        """Backward-compat shim used by main.py banned-phrase retry."""
        return self._session_mgr.get(thread_id)

    async def _get_llm(self) -> Optional[BaseChatModel]:
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

    # ──────────────────────────────────────────────────────────────────────────
    # Main pipeline
    # ──────────────────────────────────────────────────────────────────────────
    async def stream(
        self,
        query: str,
        context_id: str,
        tools: Sequence[BaseTool] | None = None,
    ) -> AsyncGenerator[dict, None]:

        yield {"is_task_complete": False, "require_user_input": False, "content": "Thinking..."}

        try:
            # ── STEP 1: Get session (read-only) ──────────────────────────────
            session = self._session_mgr.get(context_id)

            # ── STEP 2: Extract entities (current query WINS, session fills gaps)
            entities = extract_entities(query, session)

            # ── STEP 3: Understand business objective ─────────────────────────
            objective_result = understand_query(query, entities)
            objective = objective_result["objective"]

            # ── STEP 3b: Follow-up objective inheritance ──────────────────────
            # If this is a follow-up (e.g. "what about others") and the understander
            # returned MATERIAL_LOOKUP, inherit the last known objective from session
            # so we repeat the same analysis for the remaining materials.
            if (objective_result.get("is_followup")
                    and objective == QueryObjective.MATERIAL_LOOKUP
                    and session.get("last_objective")
                    and session["last_objective"] not in (
                        QueryObjective.CAPABILITY_QUESTION,
                        QueryObjective.GREETING,
                        QueryObjective.OUT_OF_DOMAIN,
                    )):
                objective = session["last_objective"]
                objective_result = {**objective_result, "objective": objective}
                logger.info(
                    "[agent] follow-up '%s': inherited objective %s from session",
                    objective_result.get("followup_type", ""), objective,
                )

            # ── STEP 4: Domain gate — no SAP tools needed ─────────────────────
            if objective == QueryObjective.CAPABILITY_QUESTION:
                self._session_mgr.update_objective(context_id, objective)
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": _BOMA_CAPABILITIES}
                return

            if objective == QueryObjective.GREETING:
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": _GREETING_RESPONSE}
                return

            if objective == QueryObjective.OUT_OF_DOMAIN:
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": _OUT_OF_DOMAIN_RESPONSE}
                return

            # ── STEP 5: Update session with current entities ───────────────────
            self._session_mgr.update_from_entities(context_id, entities)
            self._session_mgr.update_objective(context_id, objective)

            # ── STEP 6: Build execution plan (pass session for follow-up context)
            conversation_context = self._session_mgr.get_conversation_summary(context_id)
            plan = build_plan(objective_result, entities,
                              session=session,
                              conversation_context=conversation_context)

            if plan.error:
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": f"⚠️ {plan.error}"}
                return

            # ── STEP 6b: SAP auth pre-flight check ───────────────────────────
            # If this query needs SAP tools but no user session token is active,
            # prompt login immediately instead of failing inside the LLM pipeline.
            if objective_result.get("requires_sap_tools", True):
                from s4hana_client import _request_basic_auth
                has_user_auth = bool(_request_basic_auth.get())
                if not has_user_auth:
                    yield {
                        "is_task_complete": True,
                        "require_user_input": False,
                        "content": (
                            "🔐 **Please log in first.**\n\n"
                            "BOMA needs your SAP CIN_100 credentials to fetch live data.\n"
                            "Use the login button (top right) and enter your CIN_100 username and password."
                        ),
                    }
                    return

            # ── STEP 7: Get LLM ───────────────────────────────────────────────
            llm = await self._get_llm()
            if llm is None:
                yield {"is_task_complete": True, "require_user_input": False,
                       "content": "⚠️ AI engine not configured. Please contact support."}
                return

            # ── STEP 8+9: Build orchestration message (carries full plan context) ──
            # The dynamic plan/entity context goes into the message, not the system prompt.
            # This keeps the graph schema stable (same prompt every call = no conflicts).
            fx_rates = get_live_fx_rates()
            system_prompt = _build_system_prompt(plan, entities, fx_rates)  # for context only
            orchestration_msg = _build_orchestration_message(
                plan, entities, query, system_prompt, conversation_context
            )

            # ── STEP 10: Execute with tools ───────────────────────────────────
            # Use pre_model_hook (v6 pattern) — NOT prompt= parameter.
            tool_list = list(tools) if tools else []

            # Build per-request system prompt = base + conversation context
            _full_system_prompt = _BOMA_BASE_SYSTEM_PROMPT
            if conversation_context:
                _full_system_prompt = (
                    _BOMA_BASE_SYSTEM_PROMPT
                    + "\n\nCONVERSATION CONTEXT (from prior turns — use for follow-ups):\n"
                    + conversation_context
                    + "\n\nIMPORTANT: If the user says 'what about others', 'remaining', "
                    + "'continue' — use the 'STILL TO ANSWER' materials listed above."
                )

            # Update hook state with fresh system prompt for this request.
            # The hook closure reads from self._hook_state, so this affects the
            # cached graph immediately — no need to rebuild the graph each call.
            self._hook_state["system_prompt"] = _full_system_prompt

            def _pre_model_hook(state: dict) -> dict:
                all_msgs = list(state.get("messages", []))
                if all_msgs and isinstance(all_msgs[0], SystemMessage):
                    all_msgs = all_msgs[1:]
                # TRIM: pass only current-turn messages to the LLM.
                # Prior-turn tool results (large JSON blobs) are dropped — they are
                # already summarized in self._hook_state["system_prompt"] via
                # conversation_context. This prevents per-question token counts
                # from growing O(N) with conversation length.
                prior_count = self._hook_state["prior_msg_count"]
                current_turn_msgs = all_msgs[prior_count:]
                return {"messages": [SystemMessage(content=self._hook_state["system_prompt"])] + current_turn_msgs}

            # Build graph once per agent instance.
            # Hook reads from self._hook_state dynamically — it is always up-to-date.
            if not hasattr(self, '_graph') or self._graph is None:
                self._graph = create_react_agent(
                    llm,
                    tools=tool_list,
                    pre_model_hook=_pre_model_hook,
                    checkpointer=self._checkpointer,
                )
                logger.info("BOMA graph built with %d tools", len(tool_list))
            # Else: graph already built. self._hook_state is updated above,
            # so the cached hook automatically uses the fresh system prompt + prior_count.

            config = {"configurable": {"thread_id": context_id}}

            # Snapshot message count BEFORE this turn.
            # Used both for per-question token counting AND message trimming in the hook.
            try:
                _prior_state = await self._graph.aget_state(config)
                _prior_msg_count = len(_prior_state.values.get("messages", []))
            except Exception:
                _prior_msg_count = 0

            # Write prior_msg_count into hook state BEFORE ainvoke starts.
            # The hook reads this to know which messages belong to the current turn.
            self._hook_state["prior_msg_count"] = _prior_msg_count

            result = await self._graph.ainvoke(
                {"messages": [HumanMessage(content=orchestration_msg)]},
                config,
            )
            response = result["messages"][-1].content

            # ── STEP 11: Response validation + smart retry ────────────────────
            # If the response is clearly wrong (missing primary signal),
            # do ONE corrective retry with an explicit sharpening prompt.
            validation = validate_response(response, objective, entities, query)
            if not validation.passed:
                logger.warning("[agent] validation failed: %s — retrying with corrective prompt", validation.reason)
                corrective_msg = (
                    f"VALIDATION FAILED: {validation.reason}\n"
                    f"CORRECTION REQUIRED: {validation.fix_hint}\n\n"
                    f"Original query: {query}\n"
                    f"Expected output: {plan.primary_output}\n\n"
                    f"Please re-answer the original query correctly. "
                    f"The PRIMARY answer must be: {plan.primary_output}. "
                    f"Use the SAP tool results already retrieved."
                )
                try:
                    retry_result = await self._graph.ainvoke(
                        {"messages": [HumanMessage(content=corrective_msg)]},
                        config,
                    )
                    retry_response = retry_result["messages"][-1].content
                    # Only use retry if it's longer and doesn't have the same issue
                    if len(retry_response) > 50:
                        response = retry_response
                        logger.info("[agent] corrective retry succeeded")
                except Exception as retry_err:
                    logger.warning("[agent] corrective retry failed: %s — using original", retry_err)

            # ── STEP 12: Final cleanup ─────────────────────────────────────────
            response = strip_banned_phrases(response)

            # ── STEP 13: Store conversation turn in session ───────────────────
            self._session_mgr.add_conversation_turn(
                context_id, query, response, objective
            )
            # Mark materials from this turn as processed
            for mat in entities.get("materials", []):
                self._session_mgr.mark_material_processed(context_id, mat)

            # ── STEP 14: Clear per-request exec state ─────────────────────────
            self._session_mgr.clear_exec_state(context_id)

            # Count tokens from THIS TURN ONLY (messages added since _prior_msg_count).
            # Track input and output separately for the per-question breakdown display.
            _tokens_in = 0
            _tokens_out = 0
            _this_turn_msgs = result.get("messages", [])[_prior_msg_count:]
            for _msg in _this_turn_msgs:
                _um = getattr(_msg, "usage_metadata", None)
                if _um:
                    _tokens_in  += _um.get("input_tokens", 0)
                    _tokens_out += _um.get("output_tokens", 0)
                    if _tokens_in == 0 and _tokens_out == 0:
                        # Fallback: some providers only expose total_tokens
                        _tot = _um.get("total_tokens", 0)
                        _tokens_out += _tot  # attribute to output as conservative estimate
                else:
                    _rm = getattr(_msg, "response_metadata", None)
                    if _rm:
                        _u = _rm.get("usage", {}) or _rm.get("token_usage", {})
                        _tokens_in  += _u.get("prompt_tokens", _u.get("input_tokens", 0))
                        _tokens_out += _u.get("completion_tokens", _u.get("output_tokens", 0))
                        if _tokens_in == 0 and _tokens_out == 0:
                            _tokens_out += _u.get("total_tokens", 0)

            _tokens_total = _tokens_in + _tokens_out

            yield {"is_task_complete": True, "require_user_input": False,
                   "content": response,
                   "tokens_used": _tokens_total,
                   "tokens_in": _tokens_in,
                   "tokens_out": _tokens_out}

        except Exception as e:
            err_str = str(e).lower()
            logger.exception("[agent] pipeline error: %s: %s", type(e).__name__, e)
            # Give actionable message based on error type
            if any(k in err_str for k in ['notfound', '404', 'deployment', 'model']):
                msg = "⚠️ The AI engine is temporarily unavailable. Please try again in a moment."
            elif any(k in err_str for k in ['401', 'unauthorized', 'anmeldung', 'login', 'credential']):
                msg = "🔐 Your SAP session has expired. Please log in again using the login screen."
            elif any(k in err_str for k in ['timeout', 'timed out', 'connection']):
                msg = "⏱️ SAP CIN_100 is taking too long to respond. Please try again."
            else:
                msg = "⚠️ Something went wrong. Please try again."
            yield {
                "is_task_complete": True,
                "require_user_input": False,
                "content": msg,
            }

    async def invoke(
        self,
        query: str,
        context_id: str,
        tools: Sequence[BaseTool] | None = None,
    ) -> AgentResponse:
        import time as _time
        t0 = _time.monotonic()
        last: dict = {}
        total_tokens = 0
        total_in = 0
        total_out = 0
        async for chunk in self.stream(query, context_id, tools=tools):
            last = chunk
            total_tokens += chunk.get("tokens_used", 0)
            total_in     += chunk.get("tokens_in", 0)
            total_out    += chunk.get("tokens_out", 0)
        elapsed_ms = int((_time.monotonic() - t0) * 1000)
        if last.get("is_task_complete"):
            return AgentResponse(status="completed", message=last["content"],
                                 tokens_used=total_tokens, tokens_in=total_in,
                                 tokens_out=total_out, time_taken_ms=elapsed_ms)
        if last.get("require_user_input"):
            return AgentResponse(status="input_required", message=last["content"],
                                 tokens_used=total_tokens, tokens_in=total_in,
                                 tokens_out=total_out, time_taken_ms=elapsed_ms)
        return AgentResponse(status="error", message=last.get("content", "Unknown error"),
                             tokens_used=total_tokens, tokens_in=total_in,
                             tokens_out=total_out, time_taken_ms=elapsed_ms)


# ---------------------------------------------------------------------------
# Orchestration message builder
# ---------------------------------------------------------------------------
def _build_orchestration_message(plan: ExecutionPlan, entities: dict, original_query: str, system_context: str = "", conversation_context: str = "") -> str:
    """
    Builds a precise instruction to the LLM describing exactly what to do,
    so the LLM acts as an executor of the plan — not a free-form reasoner.
    """
    objective = plan.objective
    material = entities.get("material", "")
    plants = entities.get("plants", [])
    bom_number = entities.get("bom_number", "")
    budget = entities.get("budget")
    budget_currency = entities.get("budget_currency", "USD")

    steps_description = "\n".join(
        f"  Step {s.step}: [{s.action}] {s.tool or s.calc_type or s.format} — {s.purpose}"
        for s in plan.steps
    )

    entity_constraints = []
    if material:
        entity_constraints.append(f"material_number='{material}'")
    if plants:
        entity_constraints.append(f"plants={plants}")
    if bom_number:
        entity_constraints.append(f"bom_number='{bom_number}'")
    if budget:
        entity_constraints.append(f"budget={budget_currency} {budget:,.0f}")

    constraint_str = ", ".join(entity_constraints) or "none extracted from query"

    context_block = f"\n\n[PLANNING CONTEXT]\n{system_context}\n[END CONTEXT]\n" if system_context else ""
    conv_block    = f"\n\n[CONVERSATION MEMORY]\n{conversation_context}\n[END MEMORY]\n" if conversation_context else ""

    msg = f"""QUERY: {original_query}

BUSINESS OBJECTIVE: {objective}
PRIMARY ANSWER REQUIRED: {plan.primary_output}

ENTITIES FROM THIS QUERY: {constraint_str}

MULTI-MATERIAL: {plan.is_multi_material} — Materials to process: {plan.materials_to_process}
FOLLOW-UP: {plan.is_followup} — Process ALL pending materials listed in conversation memory.
HYPOTHETICAL: {plan.is_hypothetical} — If True, do NOT say "material not found"; estimate from components.

EXECUTION STEPS:
{steps_description}

CRITICAL EXECUTION RULES:
1. Execute ALL steps in order — do not skip any.
2. Use ONLY the entities listed above as tool arguments.
3. MULTI-MATERIAL STRICT RULE — process materials in this EXACT order: {plan.materials_to_process}
   - Start with material INDEX 0: {plan.materials_to_process[0] if plan.materials_to_process else material}
   - Then INDEX 1, INDEX 2, etc. — DO NOT skip any material.
   - Your response MUST show results for ALL {len(plan.materials_to_process) if plan.materials_to_process else 1} material(s).
   - If you only show results for some materials, your answer is INCOMPLETE.
4. Deliver the PRIMARY ANSWER as the dominant part of your response.
5. Supporting data (prices, comparisons) comes AFTER the primary answer.
6. If a material is not found: "Material X does not exist in SAP CIN_100."
7. Always show amounts in both original currency AND INR.
{context_block}{conv_block}"""

    # Add objective-specific sharpening
    if objective == QueryObjective.SAVINGS_CALCULATION and len(plants) >= 2:
        msg += f"""
SHARPENING: The user asked HOW MUCH THEY WILL SAVE.
Your answer must START with the savings amount in INR.
Format: "💰 By purchasing {material} from plant {plants[0]} instead of {plants[1]}, you save ₹X,XX,XXX (Y.Y%)"
"""

    if objective == QueryObjective.COST_VARIANCE_ANALYSIS:
        msg += f"""
SHARPENING: The user asked WHY cost differs — not WHAT the components are.
Call explain_bom_cost_variance. Your answer must identify ROOT CAUSE:
  - Which component contributes most to the price gap?
  - Is it procurement type (Make vs Buy)?
  - Is it supplier pricing?
  - Is it currency difference?
DO NOT just list BOM components.
"""

    if objective == QueryObjective.CAPABILITY_QUESTION:
        msg += "\nSHARPENING: List all BOMA capabilities. Do NOT mention only DEMOCAR."

    if objective == QueryObjective.BUDGET_ANALYSIS:
        plant = plants[0] if plants else "the specified plant"
        msg += f"""
SHARPENING: User wants to know HOW MANY UNITS they can buy.
Calculate: floor(budget_in_INR / unit_price_in_INR) for {material} at {plant}.
Show: units, total spent, remaining budget.
"""

    if objective == QueryObjective.MULTI_STEP_BUDGET:
        msg += f"""
SHARPENING: This is a SEQUENTIAL budget question.
Step 1: Buy first material → calculate units + remaining budget
Step 2: Use remaining budget → buy second material → calculate units
Show BOTH results clearly separated.
"""

    if objective == QueryObjective.RECURSIVE_BOM:
        plant_str = plants[0] if plants else "the plant"
        msg += f"""
SHARPENING: User asked for ALL LEVELS of the BOM — not just Level 1.
Use deep_bom_cost_analysis with max_depth=10 for {material} at {plant_str}.
Your response MUST show:
  ✅ Level 0: {material} (top-level assembly)
  ✅ Level 1: direct sub-assemblies/components
  ✅ Level 2: sub-components of Level 1 items
  ✅ Level 3, 4, 5... continue until NO further sub-components exist
  ✅ Leaf nodes: independent components (raw materials, bought-out parts)
  ✅ Total level count + total component count

FORBIDDEN: Do NOT stop at Level 1. Do NOT say "single-level BOM shown."
If a component has its own BOM, drill into it. Keep going until leaf level.

TOTAL COST RULE (avoid double-counting): if you report a "Total Cost" for the whole
assembly, use the tool's perPlant.totalCost as-is (it already sums LEAF-level
components only). Do NOT manually add up every line in the breakdown yourself — a
parent/assembly's own unitPrice already includes the value of its children, so
summing a parent's price AND its children's prices double- (or triple-) counts the
same value once per level. If asked to show a total, either use totalCost directly,
or sum only the lines where includedInTotal is true.
Format as indented tree:
  {material} (Level 0)
  ├── COMPONENT-A (Level 1)
  │   ├── SUB-A1 (Level 2)
  │   └── SUB-A2 (Level 2)
  └── COMPONENT-B (Level 1)
      └── SUB-B1 (Level 2) ← Leaf
"""

    if objective in (
        QueryObjective.CROSS_PLANT_OPTIMISATION,
        QueryObjective.MAKE_VS_BUY,
        QueryObjective.COST_OPTIMISATION,
        QueryObjective.HYPOTHETICAL_PLANNING,
    ):
        msg += """
SHARPENING: The user wants the CHEAPEST way to source MULTIPLE components — not the
single cheapest plant to buy everything from. Evaluate every component independently
and mix plants if that's cheaper. Show a per-component table with the chosen plant and
why, then a final total that sums each component's OWN cheapest-plant price. Do not
double-count parent + child costs if a BOM rollup is involved (use totalCost / the
includedInTotal flag, not a manual sum of every breakdown line).
"""

    if objective == QueryObjective.BOM_NUMBER_LOOKUP and plan.is_multi_material:
        mat_list = plan.materials_to_process
        msg += f"""
SHARPENING: User asked for BOM numbers for {len(mat_list)} materials: {', '.join(mat_list)}
You MUST show results for EVERY material. Strict order:
{chr(10).join(f'  {i+1}. Call get_bom_headers for {m} — show its BOM numbers' for i, m in enumerate(mat_list))}

FORBIDDEN: Do NOT start from material #2. Start from #1 ({mat_list[0] if mat_list else ''}).
If you skip any material your answer is INCOMPLETE and WRONG.
"""

    return msg

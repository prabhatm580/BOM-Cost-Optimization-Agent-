"""
BOMA v2.1 — Layer 3: Planner (Enhanced)
=========================================
Builds a deterministic execution graph from (objective + entities).

ENHANCEMENTS vs v2.0:
  - Multi-material support (processes ALL materials, not just first)
  - Follow-up resolution (inherits context from session)
  - RECURSIVE_BOM — auto-traverses full hierarchy
  - HYPOTHETICAL_PLANNING — cost estimate for new/non-existent materials
  - CROSS_PLANT_OPTIMISATION — globally cheapest sourcing
  - PROCUREMENT_STRATEGY — full procurement analysis
  - Better plant auto-discovery when none specified

The planner is driven by BUSINESS OBJECTIVE — not keywords.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from query_understander import QueryObjective

logger = logging.getLogger(__name__)


@dataclass
class PlanStep:
    step: int
    action: str            # "tool" | "calculate" | "respond" | "validate"
    tool: Optional[str] = None
    args: dict = field(default_factory=dict)
    calc_type: Optional[str] = None
    inputs_from: list[int] = field(default_factory=list)
    format: Optional[str] = None
    purpose: str = ""
    primary_output: str = ""


@dataclass
class ExecutionPlan:
    objective: str
    steps: list[PlanStep]
    primary_output: str
    requires_multi_tool: bool
    validation_required: bool
    entities: dict
    error: Optional[str] = None
    # v2.1+
    materials_to_process: list[str] = field(default_factory=list)
    is_multi_material: bool = False
    is_followup: bool = False
    is_hypothetical: bool = False
    conversation_context: str = ""


def build_plan(
    objective_result: dict,
    entities: dict,
    session: dict | None = None,
    conversation_context: str = "",
    thread_id: str | None = None,
) -> ExecutionPlan:
    """
    Build execution plan from query objective + extracted entities.
    Returns ExecutionPlan with ordered steps.

    session: used to resolve follow-up queries (which materials to process)
    conversation_context: injected from session manager for LLM context
    """
    objective  = objective_result["objective"]
    material   = entities.get("material")
    plants     = entities.get("plants", [])
    bom_number = entities.get("bom_number")
    budget     = entities.get("budget")
    budget_currency = entities.get("budget_currency", "USD")
    primary_output  = objective_result.get("primary_output", "")
    is_followup     = entities.get("is_followup", False) or objective_result.get("is_followup", False)
    followup_type   = entities.get("followup_type", "") or objective_result.get("followup_type", "")
    is_multi_material = objective_result.get("is_multi_material", False)
    is_hypothetical   = objective_result.get("is_hypothetical", False)
    all_materials  = entities.get("materials", [])
    hyp_components = entities.get("hypothetical_components", [])

    # ── Resolve follow-up context from session ──────────────────────────────
    if is_followup and session:
        # Inherit plants from session if not in current query
        if not plants and session.get("plants"):
            plants = session["plants"]
            entities = {**entities, "plants": plants}

        # Resolve pending materials for follow-up types
        all_mentioned = session.get("materials_mentioned", [])
        processed     = set(session.get("materials_processed", []))
        pending       = [m for m in all_mentioned if m not in processed]

        if followup_type in ("others", "remaining", "rest") and pending:
            all_materials = pending
            material      = pending[0]
            entities      = {**entities, "material": material, "materials": all_materials}
        elif followup_type in ("all_of_them", "same_for_all") and all_mentioned:
            all_materials = all_mentioned
            material      = all_mentioned[0]
            entities      = {**entities, "material": material, "materials": all_materials}
        elif followup_type == "continue" and pending:
            all_materials = pending[:1]
            material      = pending[0]
            entities      = {**entities, "material": material, "materials": all_materials}
        elif not all_materials and session.get("materials_mentioned"):
            all_materials = session.get("materials_mentioned", [])
            if not material and all_materials:
                material = all_materials[0]
            entities = {**entities, "material": material, "materials": all_materials}

    # ── Materials to process for this plan ──────────────────────────────────
    materials_to_process = all_materials if all_materials else ([material] if material else [])

    # ── Route to plan builder ────────────────────────────────────────────────
    if objective == QueryObjective.CAPABILITY_QUESTION:
        return _plan_capability()

    if objective == QueryObjective.GREETING:
        return _plan_greeting()

    if objective == QueryObjective.OUT_OF_DOMAIN:
        return _plan_out_of_domain()

    if objective == QueryObjective.REVERSE_BOM_LOOKUP:
        return _plan_reverse_bom(bom_number, entities, conversation_context)

    if objective == QueryObjective.BOM_NUMBER_LOOKUP:
        return _plan_bom_number_lookup(
            materials_to_process, plants, entities,
            is_multi_material, conversation_context
        )

    if objective == QueryObjective.BOM_EXPLOSION:
        return _plan_bom_explosion(material, plants, entities, conversation_context)

    if objective == QueryObjective.RECURSIVE_BOM:
        return _plan_recursive_bom(material, plants, entities, conversation_context)

    if objective == QueryObjective.COST_VARIANCE_ANALYSIS:
        return _plan_cost_variance(material, plants, entities, conversation_context)

    if objective == QueryObjective.SAVINGS_CALCULATION:
        return _plan_savings(material, plants, entities, conversation_context)

    if objective == QueryObjective.PRICE_COMPARISON:
        return _plan_price_comparison(material, plants, entities, conversation_context)

    if objective == QueryObjective.PRICE_LOOKUP:
        return _plan_price_lookup(material, plants, entities, conversation_context)

    if objective == QueryObjective.BUDGET_ANALYSIS:
        return _plan_budget(material, plants, budget, budget_currency, entities, conversation_context)

    if objective == QueryObjective.MULTI_STEP_BUDGET:
        return _plan_multi_step_budget(entities, conversation_context)

    if objective == QueryObjective.MAKE_VS_BUY:
        return _plan_make_vs_buy(material, plants, entities, conversation_context)

    if objective == QueryObjective.COST_OPTIMISATION:
        return _plan_cost_optimisation(material, plants, entities, conversation_context)

    if objective == QueryObjective.PROCUREMENT_STRATEGY:
        return _plan_procurement_strategy(material, plants, entities, conversation_context)

    if objective == QueryObjective.CROSS_PLANT_OPTIMISATION:
        return _plan_cross_plant_optimisation(
            materials_to_process, plants, entities,
            is_multi_material, conversation_context
        )

    if objective == QueryObjective.HYPOTHETICAL_PLANNING:
        return _plan_hypothetical(material, plants, budget, budget_currency,
                                   hyp_components, entities, conversation_context)

    if objective == QueryObjective.PLANT_DISCOVERY:
        return _plan_plant_discovery(material, entities, conversation_context)

    if objective == QueryObjective.CURRENCY_CONVERSION:
        return _plan_fx(entities)

    # Fallback
    return _plan_general(material, plants, bom_number, entities, conversation_context)


# ────────────────────────────────────────────────────────────────────────────
# Individual plan builders
# ────────────────────────────────────────────────────────────────────────────

def _plan_capability() -> ExecutionPlan:
    return ExecutionPlan(
        objective=QueryObjective.CAPABILITY_QUESTION,
        primary_output="BOMA capability list",
        requires_multi_tool=False, validation_required=False, entities={},
        steps=[PlanStep(step=1, action="respond", format="capability_answer",
                        purpose="List all BOMA capabilities without calling any SAP tool")],
    )


def _plan_greeting() -> ExecutionPlan:
    return ExecutionPlan(
        objective=QueryObjective.GREETING,
        primary_output="Greeting",
        requires_multi_tool=False, validation_required=False, entities={},
        steps=[PlanStep(step=1, action="respond", format="greeting",
                        purpose="Respond to greeting, introduce BOMA")],
    )


def _plan_out_of_domain() -> ExecutionPlan:
    return ExecutionPlan(
        objective=QueryObjective.OUT_OF_DOMAIN,
        primary_output="Refusal with BOMA scope explanation",
        requires_multi_tool=False, validation_required=False, entities={},
        steps=[PlanStep(step=1, action="respond", format="out_of_domain",
                        purpose="Politely decline, explain BOMA is SAP BOM & Cost specialist")],
    )


def _plan_reverse_bom(bom_number: Optional[str], entities: dict, ctx: str = "") -> ExecutionPlan:
    if not bom_number:
        return _error_plan("No BOM number found in your query. Please provide a BOM number like 00000052.")
    plan = ExecutionPlan(
        objective=QueryObjective.REVERSE_BOM_LOOKUP,
        primary_output="Material and plant for the given BOM number",
        requires_multi_tool=False, validation_required=True, entities=entities,
        conversation_context=ctx,
        steps=[
            PlanStep(step=1, action="tool", tool="get_material_by_bom_number",
                     args={"bom_number": bom_number},
                     purpose=f"Find material and plant for BOM {bom_number}"),
            PlanStep(step=2, action="respond", format="reverse_bom_result",
                     inputs_from=[1],
                     purpose="Show material, plant, category, status"),
        ],
    )
    return plan


def _plan_bom_number_lookup(
    materials: list, plants: list, entities: dict,
    is_multi_material: bool = False, ctx: str = "",
) -> ExecutionPlan:
    """Multi-material aware BOM number lookup — processes ALL materials in order."""
    if not materials:
        return _error_plan("Please specify a material number to look up BOM numbers.")

    # Build one tool step per material — numbered explicitly so LLM can't skip any
    steps: list[PlanStep] = []
    step_num = 1
    for idx, mat in enumerate(materials):
        args: dict = {"material_number": mat}
        if len(plants) == 1:
            args["plant"] = plants[0]
        plant_desc = f" at plant {plants[0]}" if len(plants) == 1 else " across all plants"
        steps.append(PlanStep(
            step=step_num,
            action="tool",
            tool="get_bom_headers",
            args=args,
            purpose=(
                f"[MATERIAL {idx + 1} OF {len(materials)}] "
                f"Fetch BOM numbers for {mat}{plant_desc}. "
                f"THIS IS MATERIAL #{idx + 1} — do NOT skip it."
            ),
        ))
        step_num += 1

    steps.append(PlanStep(
        step=step_num,
        action="respond",
        format="bom_numbers_table",
        inputs_from=list(range(1, step_num)),
        purpose=(
            f"Display BOM number results for ALL {len(materials)} materials: "
            f"{', '.join(f'#{i+1}={m}' for i, m in enumerate(materials))}. "
            f"Show a separate section for EACH material. "
            f"If you only show {len(materials) - 1} or fewer materials your answer is WRONG."
        ),
    ))

    primary_output = (
        f"BOM numbers for ALL {len(materials)} materials: {', '.join(materials)}"
        + (f" at plant {plants[0]}" if len(plants) == 1 else " across all plants")
    )
    return ExecutionPlan(
        objective=QueryObjective.BOM_NUMBER_LOOKUP,
        primary_output=primary_output,
        requires_multi_tool=len(materials) > 1,
        validation_required=True,
        entities=entities,
        materials_to_process=materials,
        is_multi_material=is_multi_material or len(materials) > 1,
        conversation_context=ctx,
        steps=steps,
    )


def _plan_bom_explosion(
    material: Optional[str], plants: list, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    if not material:
        return _error_plan("Please specify a material number to explode the BOM.")
    plant = plants[0] if plants else None
    if not plant:
        return ExecutionPlan(
            objective=QueryObjective.BOM_EXPLOSION,
            primary_output="BOM components",
            requires_multi_tool=True, validation_required=True, entities=entities,
            conversation_context=ctx,
            steps=[
                PlanStep(step=1, action="tool", tool="list_material_plants",
                         args={"material_number": material},
                         purpose=f"Discover plants for {material}"),
                PlanStep(step=2, action="tool", tool="get_bom_explosion",
                         args={"material_number": material},
                         inputs_from=[1], purpose="Explode BOM at discovered plant"),
                PlanStep(step=3, action="respond", format="bom_tree",
                         inputs_from=[2], purpose="Show component tree"),
            ],
        )
    return ExecutionPlan(
        objective=QueryObjective.BOM_EXPLOSION,
        primary_output=f"BOM components for {material} at plant {plant}",
        requires_multi_tool=False, validation_required=True, entities=entities,
        conversation_context=ctx,
        steps=[
            PlanStep(step=1, action="tool", tool="get_bom_explosion",
                     args={"material_number": material, "plant": plant},
                     purpose=f"Explode BOM for {material} at {plant}"),
            PlanStep(step=2, action="respond", format="bom_tree",
                     inputs_from=[1], purpose="Show component tree"),
        ],
    )


def _plan_recursive_bom(
    material: Optional[str], plants: list, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    """
    Full recursive BOM traversal — traverses EVERY child level until leaf nodes.
    Uses deep_bom_cost_analysis with max_depth=10 to guarantee full depth.
    """
    if not material:
        return _error_plan("Please specify a material number for full BOM hierarchy.")
    plant = plants[0] if plants else None

    _RECURSIVE_PURPOSE = (
        "Explode COMPLETE BOM hierarchy for {mat} at {plnt}. "
        "max_depth=10 means traverse ALL levels: Level 0 (top), Level 1, Level 2 ... "
        "until NO more sub-components exist (leaf/independent components). "
        "Do NOT stop at Level 1. Go all the way to raw material level."
    )
    _RESPOND_PURPOSE = (
        "Show the COMPLETE BOM tree for ALL levels from Level 0 down to leaf nodes. "
        "Format as indented tree: Level 0 → Level 1 → Level 2 → ... → Leaf. "
        "Count total levels found. List leaf components (no further BOM) separately. "
        "DO NOT truncate at Level 1 — show every single level."
    )

    steps: list[PlanStep] = []
    if not plant:
        steps.append(PlanStep(
            step=1, action="tool", tool="list_material_plants",
            args={"material_number": material},
            purpose=f"Discover plants for {material} — use first plant found for explosion",
        ))
        steps.append(PlanStep(
            step=2, action="tool", tool="deep_bom_cost_analysis",
            args={"material_number": material, "max_depth": 10},
            inputs_from=[1],
            purpose=_RECURSIVE_PURPOSE.format(mat=material, plnt="discovered plant"),
        ))
        steps.append(PlanStep(
            step=3, action="respond", format="recursive_bom_tree",
            inputs_from=[2],
            purpose=_RESPOND_PURPOSE,
        ))
    else:
        steps = [
            PlanStep(
                step=1, action="tool", tool="deep_bom_cost_analysis",
                args={"material_number": material, "plants": [plant], "max_depth": 10},
                purpose=_RECURSIVE_PURPOSE.format(mat=material, plnt=plant),
            ),
            PlanStep(
                step=2, action="respond", format="recursive_bom_tree",
                inputs_from=[1],
                purpose=_RESPOND_PURPOSE,
            ),
        ]
    return ExecutionPlan(
        objective=QueryObjective.RECURSIVE_BOM,
        primary_output=(
            f"Complete BOM hierarchy for {material} at {plant or 'discovered plant'} — "
            f"ALL levels from Level 0 to leaf/independent components"
        ),
        requires_multi_tool=True,
        validation_required=True,
        entities=entities,
        conversation_context=ctx,
        steps=steps,
    )


def _plan_cost_variance(
    material: Optional[str], plants: list, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    if not material:
        return _error_plan("Please specify a material number for cost variance analysis.")
    if len(plants) < 2:
        return ExecutionPlan(
            objective=QueryObjective.COST_VARIANCE_ANALYSIS,
            primary_output="Cost variance root cause",
            requires_multi_tool=True, validation_required=True, entities=entities,
            conversation_context=ctx,
            steps=[
                PlanStep(step=1, action="tool", tool="list_material_plants",
                         args={"material_number": material},
                         purpose=f"Discover plants for {material}"),
                PlanStep(step=2, action="tool", tool="explain_bom_cost_variance",
                         args={"material_number": material},
                         inputs_from=[1],
                         purpose="Recursive cost variance drill-down across plants"),
                PlanStep(step=3, action="respond", format="cost_variance_report",
                         inputs_from=[2],
                         purpose="Root cause analysis — WHY cost differs, top drivers, recommendations"),
            ],
        )
    plant1, plant2 = plants[0], plants[1]
    return ExecutionPlan(
        objective=QueryObjective.COST_VARIANCE_ANALYSIS,
        primary_output=f"Why {material} costs differ between plant {plant1} and {plant2}",
        requires_multi_tool=True, validation_required=True, entities=entities,
        conversation_context=ctx,
        steps=[
            PlanStep(step=1, action="tool", tool="explain_bom_cost_variance",
                     args={"material_number": material, "plant1": plant1, "plant2": plant2},
                     purpose=f"Recursive cost variance drill-down: {plant1} vs {plant2}"),
            PlanStep(step=2, action="respond", format="cost_variance_report",
                     inputs_from=[1],
                     purpose="Root cause: what component, what level, what reason"),
        ],
    )


def _plan_savings(
    material: Optional[str], plants: list, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    if not material:
        return _error_plan("Please specify a material number to calculate savings.")
    if len(plants) < 2:
        return ExecutionPlan(
            objective=QueryObjective.SAVINGS_CALCULATION,
            primary_output=f"Savings amount for {material} between cheapest and most expensive plant",
            requires_multi_tool=True, validation_required=False, entities=entities,
            conversation_context=ctx,
            steps=[
                PlanStep(step=1, action="tool", tool="list_material_plants",
                         args={"material_number": material},
                         purpose=f"Discover all plants for {material}"),
                PlanStep(step=2, action="tool", tool="get_material_valuation",
                         args={"material_number": material},
                         inputs_from=[1], purpose="Get prices at all plants"),
                PlanStep(step=3, action="tool", tool="get_tcurr_fx_rate",
                         args={"from_currency": "USD", "to_currency": "INR"},
                         purpose="Get FX rate"),
                PlanStep(step=4, action="calculate", calc_type="savings", inputs_from=[2, 3],
                         purpose="Calculate max savings between cheapest and most expensive plant"),
                PlanStep(step=5, action="respond", format="savings_report",
                         inputs_from=[2, 3, 4], primary_output="SAVINGS AMOUNT IN INR"),
            ],
        )
    plant1, plant2 = plants[0], plants[1]
    return ExecutionPlan(
        objective=QueryObjective.SAVINGS_CALCULATION,
        primary_output=f"Savings amount if purchasing {material} from cheaper plant (INR + %)",
        requires_multi_tool=True, validation_required=False, entities=entities,
        conversation_context=ctx,
        steps=[
            PlanStep(step=1, action="tool", tool="get_material_valuation",
                     args={"material_number": material, "plant": plant1},
                     purpose=f"Get price at plant {plant1}"),
            PlanStep(step=2, action="tool", tool="get_material_valuation",
                     args={"material_number": material, "plant": plant2},
                     purpose=f"Get price at plant {plant2}"),
            PlanStep(step=3, action="tool", tool="get_tcurr_fx_rate",
                     args={"from_currency": "USD", "to_currency": "INR"},
                     purpose="Get USD→INR rate"),
            PlanStep(step=4, action="calculate", calc_type="savings", inputs_from=[1, 2, 3],
                     purpose=f"Calculate savings = |price_{plant1} - price_{plant2}| in INR"),
            PlanStep(step=5, action="respond", format="savings_report",
                     inputs_from=[1, 2, 3, 4],
                     primary_output="SAVINGS AMOUNT IN INR — price table is secondary",
                     purpose="Primary output is savings. Price comparison is supporting context."),
        ],
    )


def _plan_price_comparison(
    material: Optional[str], plants: list, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    if not material:
        return _error_plan("Please specify a material number for price comparison.")
    if len(plants) < 2:
        return ExecutionPlan(
            objective=QueryObjective.PRICE_COMPARISON,
            primary_output=f"Price comparison for {material} across plants",
            requires_multi_tool=True, validation_required=True, entities=entities,
            conversation_context=ctx,
            steps=[
                PlanStep(step=1, action="tool", tool="list_material_plants",
                         args={"material_number": material},
                         purpose=f"Discover all plants for {material}"),
                PlanStep(step=2, action="tool", tool="get_material_valuation",
                         args={"material_number": material},
                         inputs_from=[1], purpose="Get price at each discovered plant"),
                PlanStep(step=3, action="tool", tool="get_tcurr_fx_rate",
                         args={"from_currency": "USD", "to_currency": "INR"},
                         purpose="Get FX rate for INR conversion"),
                PlanStep(step=4, action="respond", format="price_comparison_table",
                         inputs_from=[2, 3], purpose="Ranked price table with INR conversion"),
            ],
        )
    steps = []
    for i, plant in enumerate(plants[:4], start=1):
        steps.append(PlanStep(step=i, action="tool", tool="get_material_valuation",
                               args={"material_number": material, "plant": plant},
                               purpose=f"Get price at plant {plant}"))
    n = len(steps)
    steps.append(PlanStep(step=n+1, action="tool", tool="get_tcurr_fx_rate",
                           args={"from_currency": "USD", "to_currency": "INR"},
                           purpose="Get FX rate for INR conversion"))
    steps.append(PlanStep(step=n+2, action="respond", format="price_comparison_table",
                           inputs_from=list(range(1, n+2)), purpose="Ranked price table"))
    return ExecutionPlan(
        objective=QueryObjective.PRICE_COMPARISON,
        primary_output=f"Ranked price comparison for {material}",
        requires_multi_tool=True, validation_required=False, entities=entities,
        conversation_context=ctx, steps=steps,
    )


def _plan_price_lookup(
    material: Optional[str], plants: list, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    if not material:
        return _error_plan("Please specify a material number.")
    plant = plants[0] if plants else None
    if not plant:
        return _plan_price_comparison(material, plants, entities, ctx)
    return ExecutionPlan(
        objective=QueryObjective.PRICE_LOOKUP,
        primary_output=f"Price of {material} at plant {plant}",
        requires_multi_tool=False, validation_required=False, entities=entities,
        conversation_context=ctx,
        steps=[
            PlanStep(step=1, action="tool", tool="get_material_valuation",
                     args={"material_number": material, "plant": plant},
                     purpose=f"Get price of {material} at {plant}"),
            PlanStep(step=2, action="respond", format="price_card",
                     inputs_from=[1], purpose="Show price with INR conversion"),
        ],
    )


def _plan_budget(
    material: Optional[str], plants: list, budget: Optional[float],
    budget_currency: str, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    if not material:
        return _error_plan("Please specify a material number for budget analysis.")
    plant = plants[0] if plants else None
    if not budget:
        return _error_plan("Please specify your budget (e.g. '2 CR USD', '500 lakh INR').")

    if not plant:
        # No plant specified — automatically find cheapest across all plants
        return ExecutionPlan(
            objective=QueryObjective.BUDGET_ANALYSIS,
            primary_output=f"How many {material} from cheapest plant with {budget_currency} {budget:,.0f}",
            requires_multi_tool=True, validation_required=False, entities=entities,
            conversation_context=ctx,
            steps=[
                PlanStep(step=1, action="tool", tool="get_cheapest_material_valuation",
                         args={"material_number": material},
                         purpose=(
                             f"Find cheapest plant and price for {material} across ALL plants. "
                             f"Returns cheapestPlant, cheapestPriceINR (already in INR), rankedPlants."
                         )),
                PlanStep(step=2, action="tool", tool="run_cost_calculator",
                         args={
                             "components": f"<<[{{name:{material}, qty:1, price:cheapestPriceINR_from_step1, currency:'INR', plant:cheapestPlant_from_step1}}]>>",
                             "budget": budget,
                             "budget_currency": budget_currency,
                         },
                         purpose=(
                             "Deterministic floor division: units = floor(budget_INR / cheapestPriceINR). "
                             "Read units_affordable, total_spent_inr, remaining_inr from result."
                         )),
                PlanStep(step=3, action="respond", format="budget_report", inputs_from=[1, 2],
                         purpose="Report: which plant is cheapest, units affordable, total spent, remaining."),
            ],
        )

    return ExecutionPlan(
        objective=QueryObjective.BUDGET_ANALYSIS,
        primary_output=f"How many {material} you can buy from plant {plant} with {budget_currency} {budget:,.0f}",
        requires_multi_tool=True, validation_required=False, entities=entities,
        conversation_context=ctx,
        steps=[
            PlanStep(step=1, action="tool", tool="get_material_valuation",
                     args={"material_number": material, "plant": plant},
                     purpose=f"Get unit price of {material} at plant {plant}"),
            PlanStep(step=2, action="tool", tool="run_cost_calculator",
                     args={
                         "components": f"<<[{{name:{material}, qty:1, price:unit_price_from_step1, currency:currency_from_step1, plant:{plant}}}]>>",
                         "budget": budget,
                         "budget_currency": budget_currency,
                     },
                     purpose="Deterministic floor division: read units_affordable, total_spent_inr, remaining_inr"),
            PlanStep(step=3, action="respond", format="budget_report", inputs_from=[1, 2],
                     purpose="Units affordable + remaining budget + recommendation"),
        ],
    )


def _plan_multi_step_budget(entities: dict, ctx: str = "") -> ExecutionPlan:
    return ExecutionPlan(
        objective=QueryObjective.MULTI_STEP_BUDGET,
        primary_output="Units of material A, then units of material B from remaining budget",
        requires_multi_tool=True, validation_required=False, entities=entities,
        conversation_context=ctx,
        steps=[
            PlanStep(step=1, action="tool", tool="get_material_valuation", args={},
                     purpose="Get unit price of primary material at specified plant"),
            PlanStep(step=2, action="calculate", calc_type="budget_units_step1", inputs_from=[1],
                     purpose="Calculate units of primary material + remaining budget"),
            PlanStep(step=3, action="tool", tool="get_material_valuation", args={},
                     purpose="Get unit price of secondary material at specified plant"),
            PlanStep(step=4, action="calculate", calc_type="budget_units_step2", inputs_from=[2, 3],
                     purpose="Calculate units of secondary material from remaining budget"),
            PlanStep(step=5, action="respond", format="multi_step_budget_report",
                     inputs_from=[1, 2, 3, 4],
                     primary_output="Step 1 result + Step 2 result, clearly separated"),
        ],
    )


def _plan_make_vs_buy(
    material: Optional[str], plants: list, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    if not material:
        return _error_plan("Please specify a material for Make vs Buy analysis.")
    return ExecutionPlan(
        objective=QueryObjective.MAKE_VS_BUY,
        primary_output=f"Make vs Buy recommendation for {material}",
        requires_multi_tool=True, validation_required=False, entities=entities,
        conversation_context=ctx,
        steps=[
            PlanStep(step=1, action="tool", tool="list_material_plants",
                     args={"material_number": material},
                     purpose=f"Discover plants for {material}"),
            PlanStep(step=2, action="tool", tool="get_material_procurement_type",
                     args={"material_number": material},
                     inputs_from=[1], purpose="Get Make/Buy type per plant"),
            PlanStep(step=3, action="tool", tool="get_material_valuation",
                     args={"material_number": material},
                     inputs_from=[1], purpose="Get prices to compare Make vs Buy cost"),
            PlanStep(step=4, action="respond", format="make_buy_report",
                     inputs_from=[2, 3],
                     purpose="Make vs Buy recommendation with cost justification"),
        ],
    )


def _plan_cost_optimisation(
    material: Optional[str], plants: list, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    if not material:
        return _error_plan("Please specify a material for cost optimisation.")
    return ExecutionPlan(
        objective=QueryObjective.COST_OPTIMISATION,
        primary_output=f"Cost optimisation actions and savings for {material}",
        requires_multi_tool=True, validation_required=False, entities=entities,
        conversation_context=ctx,
        steps=[
            PlanStep(step=1, action="tool", tool="list_material_plants",
                     args={"material_number": material},
                     purpose=f"Discover all plants for {material}"),
            PlanStep(step=2, action="tool", tool="deep_bom_cost_analysis",
                     args={"material_number": material},
                     inputs_from=[1], purpose="Full BOM cost explosion across all plants"),
            PlanStep(step=3, action="respond", format="optimisation_report",
                     inputs_from=[2],
                     purpose="Actions + savings per component + recommendations"),
        ],
    )


def _plan_procurement_strategy(
    material: Optional[str], plants: list, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    """
    Procurement strategy: how is this material sourced, at which plants,
    what type (E/F/X), suppliers, recommendation.
    """
    if not material:
        return _error_plan("Please specify a material for procurement strategy analysis.")
    steps: list[PlanStep] = [
        PlanStep(step=1, action="tool", tool="list_material_plants",
                 args={"material_number": material},
                 purpose=f"Discover all plants for {material}"),
        PlanStep(step=2, action="tool", tool="get_material_procurement_type",
                 args={"material_number": material},
                 inputs_from=[1], purpose="Get procurement type (E/F/X) at each plant"),
        PlanStep(step=3, action="tool", tool="get_material_valuation",
                 args={"material_number": material},
                 inputs_from=[1], purpose="Get prices at all plants for cost context"),
    ]
    if plants:
        steps.append(PlanStep(step=4, action="tool", tool="get_purchase_info_records",
                               args={"material_number": material, "plant": plants[0]},
                               purpose="Get supplier/vendor information"))
    steps.append(PlanStep(
        step=len(steps)+1, action="respond", format="procurement_strategy_report",
        inputs_from=list(range(1, len(steps)+1)),
        purpose="Procurement strategy summary with plant-level detail and recommendation",
    ))
    return ExecutionPlan(
        objective=QueryObjective.PROCUREMENT_STRATEGY,
        primary_output=f"Procurement strategy for {material} across all plants",
        requires_multi_tool=True, validation_required=False, entities=entities,
        conversation_context=ctx, steps=steps,
    )


def _plan_cross_plant_optimisation(
    materials: list, plants: list, entities: dict,
    is_multi_material: bool = False, ctx: str = "",
) -> ExecutionPlan:
    """
    Cross-plant optimisation: evaluate ALL available plants, choose globally cheapest combo.
    Explains WHY, COST DIFFERENCE, SAVINGS, MAJOR COST DRIVERS, ALTERNATIVES.
    """
    if not materials:
        return _error_plan("Please specify at least one material for cross-plant optimisation.")

    material = materials[0]
    steps: list[PlanStep] = [
        PlanStep(step=1, action="tool", tool="list_material_plants",
                 args={"material_number": material},
                 purpose=f"Discover ALL plants for {material} in SAP CIN_100"),
        PlanStep(step=2, action="tool", tool="deep_bom_cost_analysis",
                 args={"material_number": material},
                 inputs_from=[1],
                 purpose="Full cost analysis across ALL discovered plants, with BOM explosion"),
        PlanStep(step=3, action="tool", tool="get_tcurr_fx_rate",
                 args={"from_currency": "USD", "to_currency": "INR"},
                 purpose="Get FX rate for global cost comparison in INR"),
        PlanStep(step=4, action="respond", format="cross_plant_optimisation_report",
                 inputs_from=[1, 2, 3],
                 purpose=(
                     "Globally optimal procurement recommendation: "
                     "WHY this plant is best, cost difference, savings amount, "
                     "major cost drivers, better alternatives if any"
                 )),
    ]
    return ExecutionPlan(
        objective=QueryObjective.CROSS_PLANT_OPTIMISATION,
        primary_output=f"Globally optimised procurement for {material} — best plant + savings explained",
        requires_multi_tool=True, validation_required=False, entities=entities,
        materials_to_process=materials,
        is_multi_material=is_multi_material,
        conversation_context=ctx, steps=steps,
    )


def _plan_hypothetical(
    material: Optional[str], plants: list,
    budget: Optional[float], budget_currency: str,
    hyp_components: list, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    """
    Hypothetical / new-material planning.
    Material doesn't exist yet — user provides component list.
    If a specific plant is given: use get_material_valuation per component (skip missing).
    Otherwise: use get_cheapest_material_valuation (auto-discovers cheapest plant per component).
    """
    component_materials = [c["material"] for c in hyp_components] if hyp_components else []
    steps: list[PlanStep] = []
    step_num = 1
    forced_plant = plants[0] if plants else None  # user wants all from this one plant

    if component_materials:
        for comp in component_materials[:6]:  # cap at 6 components
            if forced_plant:
                steps.append(PlanStep(
                    step=step_num, action="tool", tool="get_material_valuation",
                    args={"material_number": comp, "plant": forced_plant},
                    purpose=(
                        f"Get price of component {comp} at plant {forced_plant}. "
                        f"If component NOT found at this plant: SKIP it (do not include it with price 0). "
                        f"Note which components were found vs skipped."
                    ),
                ))
            else:
                steps.append(PlanStep(
                    step=step_num, action="tool", tool="get_cheapest_material_valuation",
                    args={"material_number": comp},
                    purpose=(
                        f"Get cheapest plant and price for component {comp}. "
                        f"Returns cheapestPlant, cheapestPriceINR (already in INR, no FX needed). "
                        f"Use cheapestPriceINR directly — do NOT multiply by FX again."
                    ),
                ))
            step_num += 1
    # NOTE: NO get_tcurr_fx_rate step — run_cost_calculator handles all FX internally.
    # Adding a separate FX step causes the LLM to pre-convert budget and pass wrong currency.

    # Deterministic calculator step — build components list from prior results
    if forced_plant:
        calc_purpose = (
            "DETERMINISTIC CALCULATION: compute total assembly cost for single-plant sourcing. "
            f"Build the components list ONLY from components found at plant {forced_plant}: "
            "for each found component use {name: material_number, qty: qty_from_query, "
            "price: effectivePrice_from_valuation, currency: original_currency, plant: "
            f"'{forced_plant}'}}. "
            "SKIP any component that had no valuation at this plant (do not include it with price=0). "
            "Pass budget as a raw integer. Compare result to cheapest-sourcing cost from conversation."
        )
    else:
        calc_purpose = (
            "DETERMINISTIC CALCULATION: compute total assembly cost + units affordable. "
            "Build the components list from all get_cheapest_material_valuation results: "
            "for each component use {name: material_number, qty: quantity_from_query, "
            "price: cheapestPriceINR, currency: 'INR', plant: cheapestPlant}. "
            "IMPORTANT: Since cheapestPriceINR is already in INR, set currency='INR'. "
            "Pass budget as a raw integer (e.g. 1000000 for 1M USD, not '1M USD')."
        )
    steps.append(PlanStep(
        step=step_num, action="tool", tool="run_cost_calculator",
        args={
            "components": f"<<build from {'get_material_valuation' if forced_plant else 'get_cheapest_material_valuation'} results — SKIP missing>>",
            "extra_charges": "<<from query context>>",
            "budget": budget if budget else 0.0,
            "budget_currency": budget_currency,
        },
        purpose=calc_purpose,
    ))
    step_num += 1

    steps.append(PlanStep(
        step=step_num, action="respond", format="hypothetical_planning_report",
        inputs_from=list(range(1, step_num)),
        purpose=(
            "Hypothetical assembly planning: "
            "per-component cheapest plant (from get_cheapest_material_valuation results), "
            "total cost per assembly unit (components + extra charges), "
            "budget-to-units calculation. "
            "DO NOT say 'material not found' — this material is being planned, not yet created."
        ),
        primary_output="Assembly cost breakdown + units affordable from budget",
    ))

    mat_label = material or "new assembly"
    return ExecutionPlan(
        objective=QueryObjective.HYPOTHETICAL_PLANNING,
        primary_output=f"Assembly cost and units affordable for {mat_label}",
        requires_multi_tool=bool(component_materials),
        validation_required=False,
        entities=entities,
        is_hypothetical=True,
        conversation_context=ctx,
        steps=steps,
    )


def _plan_plant_discovery(
    material: Optional[str], entities: dict, ctx: str = "",
) -> ExecutionPlan:
    if not material:
        return _error_plan("Please specify a material number.")
    return ExecutionPlan(
        objective=QueryObjective.PLANT_DISCOVERY,
        primary_output=f"All plants where {material} exists",
        requires_multi_tool=False, validation_required=True, entities=entities,
        conversation_context=ctx,
        steps=[
            PlanStep(step=1, action="tool", tool="list_material_plants",
                     args={"material_number": material},
                     purpose=f"Discover all plants for {material}"),
            PlanStep(step=2, action="respond", format="plant_list",
                     inputs_from=[1], purpose="Show plant list"),
        ],
    )


def _plan_fx(entities: dict) -> ExecutionPlan:
    return ExecutionPlan(
        objective=QueryObjective.CURRENCY_CONVERSION,
        primary_output="Exchange rate and converted amount",
        requires_multi_tool=False, validation_required=False, entities=entities,
        steps=[
            PlanStep(step=1, action="tool", tool="get_tcurr_fx_rate",
                     args={}, purpose="Fetch live FX rate from SAP TCURR"),
            PlanStep(step=2, action="respond", format="fx_result",
                     inputs_from=[1], purpose="Show rate and converted amount"),
        ],
    )


def _plan_general(
    material, plants, bom_number, entities: dict, ctx: str = "",
) -> ExecutionPlan:
    """Fallback plan when objective is unclear."""
    steps = []
    if material and plants:
        steps.append(PlanStep(step=1, action="tool", tool="get_material_valuation",
                               args={"material_number": material, "plant": plants[0]},
                               purpose=f"Get general info for {material}"))
    elif material:
        steps.append(PlanStep(step=1, action="tool", tool="list_material_plants",
                               args={"material_number": material},
                               purpose=f"Discover plants for {material}"))
    elif bom_number:
        steps.append(PlanStep(step=1, action="tool", tool="get_material_by_bom_number",
                               args={"bom_number": bom_number},
                               purpose=f"Reverse lookup BOM {bom_number}"))
    if not steps:
        steps.append(PlanStep(step=1, action="respond", format="help",
                               purpose="Show help/capabilities"))
    steps.append(PlanStep(step=len(steps)+1, action="respond", format="general_response",
                           inputs_from=[1], purpose="Present result"))
    return ExecutionPlan(
        objective=QueryObjective.MATERIAL_LOOKUP,
        primary_output="General material/BOM information",
        requires_multi_tool=len(steps) > 2,
        validation_required=False, entities=entities,
        conversation_context=ctx, steps=steps,
    )


def _error_plan(message: str) -> ExecutionPlan:
    return ExecutionPlan(
        objective="ERROR",
        primary_output=message,
        requires_multi_tool=False, validation_required=False, entities={},
        error=message,
        steps=[PlanStep(step=1, action="respond", format="error", purpose=message)],
    )

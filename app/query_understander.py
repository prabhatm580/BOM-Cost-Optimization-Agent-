"""
BOMA v2.3 — Layer 2: Query Understander (Semantic Enhancement)
===============================================================
Classifies BUSINESS OBJECTIVE using full semantic intent analysis.

v2.3 Improvements:
  - Semantic intent patterns — not just keyword matching
  - Negative-signal rules to prevent false routing
  - Intent disambiguation for near-identical phrases
  - "What are BOM numbers for X, Y, Z" → BOM_NUMBER_LOOKUP (not cost)
  - "Same for all" → inherits last objective from session
  - All 12 listed use cases correctly classified
  - Reasoning chain exposed for debug
"""

from __future__ import annotations

import re
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class QueryObjective(str, Enum):
    # ── Meta / non-SAP ──────────────────────────────────────────────────────
    CAPABILITY_QUESTION      = "CAPABILITY_QUESTION"
    GREETING                 = "GREETING"
    OUT_OF_DOMAIN            = "OUT_OF_DOMAIN"

    # ── BOM lookups ─────────────────────────────────────────────────────────
    BOM_NUMBER_LOOKUP        = "BOM_NUMBER_LOOKUP"
    REVERSE_BOM_LOOKUP       = "REVERSE_BOM_LOOKUP"
    BOM_EXPLOSION            = "BOM_EXPLOSION"
    RECURSIVE_BOM            = "RECURSIVE_BOM"

    # ── Price / cost ─────────────────────────────────────────────────────────
    PRICE_LOOKUP             = "PRICE_LOOKUP"
    PRICE_COMPARISON         = "PRICE_COMPARISON"
    SAVINGS_CALCULATION      = "SAVINGS_CALCULATION"
    COST_VARIANCE_ANALYSIS   = "COST_VARIANCE_ANALYSIS"

    # ── Budget / procurement ─────────────────────────────────────────────────
    BUDGET_ANALYSIS          = "BUDGET_ANALYSIS"
    MULTI_STEP_BUDGET        = "MULTI_STEP_BUDGET"
    MAKE_VS_BUY              = "MAKE_VS_BUY"
    COST_OPTIMISATION        = "COST_OPTIMISATION"
    PROCUREMENT_STRATEGY     = "PROCUREMENT_STRATEGY"
    CROSS_PLANT_OPTIMISATION = "CROSS_PLANT_OPTIMISATION"

    # ── Planning scenarios ────────────────────────────────────────────────────
    HYPOTHETICAL_PLANNING    = "HYPOTHETICAL_PLANNING"

    # ── General ─────────────────────────────────────────────────────────────
    PLANT_DISCOVERY          = "PLANT_DISCOVERY"
    MATERIAL_LOOKUP          = "MATERIAL_LOOKUP"
    CURRENCY_CONVERSION      = "CURRENCY_CONVERSION"


# ---------------------------------------------------------------------------
# INTENT RULES — ordered most-specific → most-general
# Each entry: (phrase_list, objective, weight)
# weight: 3=strong exact phrase, 2=good indicator, 1=weak signal
# ---------------------------------------------------------------------------
_RULES: list[tuple[list[str], QueryObjective, int]] = [

    # ── OUT OF DOMAIN — checked FIRST ────────────────────────────────────────
    (["capital of", "president of", "prime minister", "weather in",
      "stock market", "cricket score", "football match",
      "write code", "hello world", "recipe for",
      " in java", " in python", "syntax of", "program to"],
     QueryObjective.OUT_OF_DOMAIN, 3),

    # ── Capability & meta ───────────────────────────────────────────────────
    (["what can you do", "what are your capabilities", "your capabilities",
      "what do you do", "who are you", "what is boma",
      "what questions can i ask", "what should i ask",
      "can you answer", "what topics", "how can you help",
      "only answer for democar", "do you answer for any material",
      "any other material", "what are you", "introduce yourself",
      "tell me about yourself", "what questions should i ask",
      "what can boma do", "list your features"],
     QueryObjective.CAPABILITY_QUESTION, 3),

    (["hello", "hi ", "hey ", "good morning", "good afternoon", "good evening",
      "namaste", "how are you", "good day"],
     QueryObjective.GREETING, 3),
    # ── Hypothetical / new-material planning + profit analysis ──────────────
    (["not yet exist", "doesn't exist yet", "does not exist yet",
      "planning a new material", "new material i want to create",
      "i'm creating a new", "i am creating a new",
      "create a new bom", "plan a bom", "hypothetical",
      "estimate cost for new", "what would it cost to build",
      "if i create", "if i build", "if i plan",
      "if i order", "if i just order", "if i buy from plant",
      "what if i order", "what if i buy", "what if i just order",
      "what if i am ordering", "what if i am just ordering",
      "ordering from plant", "order from plant",
      "i want to design", "material that i am planning",
      "proposed material", "not in sap yet",
      "will i be in profit", "will i profit", "will i make profit",
      "am i in profit", "in profit", "profit percentage",
      "profit if i", "profit from", "how much profit",
      "will i save money", "make money", "break even",
      "compare with assembling", "compare with buying",
      "cheaper to assemble", "cheaper to order",
      "better to order", "better to assemble",
      "assemble myself", "assemble it myself",
      "assembling it for free", "assembly for free",
      "just the material cost", "just the price of material",
      "no labor charge", "no additional charge",
      "free assembly", "free labor"],
     QueryObjective.HYPOTHETICAL_PLANNING, 3),

    # ── Recursive / full hierarchy BOM ───────────────────────────────────────
    (["complete hierarchy", "full hierarchy", "all levels",
      "every level", "level by level", "recursive bom",
      "multilevel bom", "multi level bom", "multi-level bom",
      "multi-level bom explosion", "all sub components",
      "deepest level", "entire bom tree", "full bom structure",
      "bom tree", "complete bom structure", "entire hierarchy",
      "lowest level components",
      # ── "all components" phrases — must go to RECURSIVE not single-level ──
      "all the components", "all components of", "all components in",
      "all sub-components", "show all components", "list all components",
      "complete component list", "full component list",
      "all items in", "all items of",
      "complete bom", "full bom", "entire bom",
      "all parts of", "all parts in",
      "0,1,2", "level 0", "from level",
      "till last", "to last", "until leaf",
      "independent components", "raw material level",
      "deepest components", "leaf components", "leaf level"],
     QueryObjective.RECURSIVE_BOM, 3),

    # ── Cost Variance Analysis — MUST come before component/BOM checks ────────
    # Semantic: user wants to know WHY cost differs, not just WHAT components are
    (["responsible for price difference", "responsible for cost difference",
      "responsible for the price", "responsible for the cost",
      "main component responsible",
      "why is cost different", "why is price different",
      "why does it cost more", "why is it more expensive",
      "why does plant", "why is plant",
      "reason for price difference", "reason for cost difference",
      "root cause of cost", "root cause of price",
      "cost driver", "price driver",
      "what drives the cost", "what drives the price",
      "what is driving the cost", "what is driving the price",
      "explain cost difference", "explain price difference",
      "explain the cost", "explain the price",
      "why more expensive", "why less expensive",
      "why is it expensive", "why does it cost",
      "cost difference between", "price difference between",
      "which component contributes most",
      "what causes the cost difference",
      "variance between plants", "cost variance",
      "price variance between", "drill down cost",
      "analyse cost difference", "analyze cost difference"],
     QueryObjective.COST_VARIANCE_ANALYSIS, 3),

    # ── Cross-plant optimisation ──────────────────────────────────────────────
    (["globally optimal", "globally optimized", "globally cheapest",
      "cheapest combination", "best plant combination",
      "optimize across all plants", "cheapest source across",
      "lowest cost globally", "best procurement globally",
      "source from any plant", "from any available plant",
      "which plant globally", "globally best sourcing"],
     QueryObjective.CROSS_PLANT_OPTIMISATION, 3),

    # ── Procurement Strategy ─────────────────────────────────────────────────
    (["procurement strategy", "what is the procurement strategy",
      "sourcing strategy", "how is it procured", "how is this procured",
      "how is the material procured", "procurement type for",
      "external or internal procurement",
      "sourced externally", "produced in-house",
      "strategic procurement plan", "procurement recommendation",
      "make vs buy strategy", "make or buy strategy",
      "buying strategy", "sourcing recommendation"],
     QueryObjective.PROCUREMENT_STRATEGY, 3),

    # ── Savings calculation ──────────────────────────────────────────────────
    # Semantic: user explicitly wants to know the SAVINGS AMOUNT
    (["how much will i save", "how much would i save", "how much can i save",
      "how much i will save", "how much i would save",
      "saving if i buy", "savings if i buy",
      "saving amount", "savings amount",
      "saving by purchasing from",
      "savings by purchasing from",
      "how much cheaper is", "how much cheaper would",
      "find the saving", "calculate saving",
      "what is the saving", "total saving",
      "cost saving between", "price saving between"],
     QueryObjective.SAVINGS_CALCULATION, 3),

    # ── Multi-step budget ────────────────────────────────────────────────────
    (["remaining amount", "remaining budget", "with the remaining",
      "left over budget", "leftover budget", "what else can i buy",
      "buy both materials", "buy x and then y",
      "use remaining to buy", "spend remaining on"],
     QueryObjective.MULTI_STEP_BUDGET, 3),

    # ── Budget / affordability ───────────────────────────────────────────────
    # Semantic: user wants to know HOW MANY UNITS they can afford
    (["how many can i buy", "how many units can i buy",
      "how many can be purchased", "how many can i purchase",
      "how many can i afford", "units affordable",
      "units i can buy", "number of units",
      "with a budget of", "with my budget",
      "with 2 crore", "with 2 cr", "with 1 cr", "with 5 cr",
      "crore usd", "crore inr", "lakh usd", "lakh inr",
      "how many democar", "how many units of",
      "buy with budget", "purchase with budget",
      "afford to buy", "can i afford",
      "buy from cheapest plant", "cheapest plant only",
      "from cheapest plant only", "buy at cheapest",
      "purchase from cheapest plant", "cheapest available plant",
      "from the cheapest plant", "from cheapest available",
      "how many units can i create", "how many can i create",
      "how many can i purchase from"],
     QueryObjective.BUDGET_ANALYSIS, 3),

    # ── BOM Number Lookup — Material → BOM# ─────────────────────────────────
    # Semantic: user wants BOM NUMBER(s) for a material
    (["bom number", "bom numbers", "bill of material number",
      "what is the bom number", "what are the bom numbers",
      "which bom number", "bom no ", "bom nos",
      "what bom", "stlnr", "bom header number",
      "tell me the bom number", "give me the bom number",
      "fetch bom number", "bom numbers for"],
     QueryObjective.BOM_NUMBER_LOOKUP, 3),

    # ── Reverse BOM — BOM# → Material ────────────────────────────────────────
    # Semantic: user gives a BOM NUMBER and wants the MATERIAL
    (["material for bom", "material against bom",
      "plant and material for bom", "plant for bom number",
      "which material has bom", "bom belongs to which material",
      "let me know plant and material for bom",
      "plant and material for bom number",
      "what material is bom", "bom is for which material",
      "material behind bom", "which material does bom belong to",
      "find material for bom", "find plant for bom"],
     QueryObjective.REVERSE_BOM_LOOKUP, 3),

    # ── BOM Explosion / Component Listing ────────────────────────────────────
    # Semantic: user wants to SEE the COMPONENTS inside a BOM
    # NOTE: "all components" is intentionally NOT here — it routes to RECURSIVE_BOM above
    (["bom components", "bom structure", "explode bom", "bom explosion",
      "what components are in", "what components does",
      "components of ", "sub-components of",
      "cs12", "cs03", "what is in the bom", "items in the bom",
      "show me the bom", "list components of",
      "list the components", "component list",
      "what parts", "what items are in",
      "show components", "give me components"],
     QueryObjective.BOM_EXPLOSION, 3),

    # ── Price Comparison ─────────────────────────────────────────────────────
    # Semantic: comparing prices across multiple plants
    (["compare price", "compare cost", "compare prices",
      "price comparison", "cost comparison",
      "price at plant", "cost at plant",
      "price in plant", "cost in plant",
      "which plant is cheaper", "which is the cheaper plant",
      "cheapest plant for", "most expensive plant",
      "price difference between plants",
      "prices across plants", "costs across plants",
      "prices across all plants", "costs across all plants",
      "price across all plants", "cost across all plants",
      "prices at all plants", "costs at all plants",
      "price at all plants", "cost at all plants",
      "prices for all plants", "what are the prices",
      "what are the costs", "show me the prices",
      "show me the costs", "get me the prices",
      "how much does it cost across", "cost in all plants",
      "price in all plants"],
     QueryObjective.PRICE_COMPARISON, 3),

    # ── Price Lookup — single material/plant ─────────────────────────────────
    (["what is the price of", "what is the cost of",
      "price for material", "cost of material",
      "standard price of", "moving average price",
      "valuation of", "what does it cost",
      "how much does it cost", "what is the rate of",
      "price at plant", "valuation at plant"],
     QueryObjective.PRICE_LOOKUP, 2),

    # ── Make vs Buy ──────────────────────────────────────────────────────────
    (["make vs buy", "make or buy", "should i make or buy",
      "should i make", "should i buy",
      "in-house vs external", "internal vs external",
      "make decision", "buy decision",
      "manufacture or procure", "produce or purchase"],
     QueryObjective.MAKE_VS_BUY, 3),

    # ── Cost Optimisation ───────────────────────────────────────────────────
    (["cost optim", "optimise cost", "optimize cost",
      "reduce cost", "cost saving recommendations",
      "cost reduction opportunities", "strategic buy",
      "where can i save cost", "cost improvement",
      "identify savings", "savings opportunities"],
     QueryObjective.COST_OPTIMISATION, 3),

    # ── Plant Discovery ──────────────────────────────────────────────────────
    (["which plants have", "which plants has",
      "what plants have", "what plants has",
      "all plants for", "plants for material",
      "across all plants", "available plants",
      "plants where material exists", "in which plants",
      "list all plants", "show all plants"],
     QueryObjective.PLANT_DISCOVERY, 3),

    # ── Currency Conversion ─────────────────────────────────────────────────
    (["fx rate", "exchange rate", "convert to inr", "convert to usd",
      "convert currency", "in inr", "in usd", "how much is",
      "currency conversion", "what is 1 usd", "what is 1 eur",
      "usd to inr", "eur to inr", "inr to usd"],
     QueryObjective.CURRENCY_CONVERSION, 3),
]


# ---------------------------------------------------------------------------
# Negative-signal rules: if these are present, downrank an objective
# E.g. if query says "BOM number" → it's NOT a BOM explosion
# ---------------------------------------------------------------------------
_NEGATIVE_SIGNALS: dict[QueryObjective, list[str]] = {
    QueryObjective.BOM_EXPLOSION: ["bom number", "stlnr", "which bom number"],
    QueryObjective.BOM_NUMBER_LOOKUP: ["explode", "components of", "items in"],
    QueryObjective.PRICE_LOOKUP: ["why", "reason", "cause", "responsible", "driver"],
    QueryObjective.PRICE_COMPARISON: ["why", "reason", "cause", "responsible", "driver"],
    QueryObjective.PLANT_DISCOVERY: [
        "prices across", "costs across", "what are the prices",
        "what are the costs", "show me the prices", "show me the costs",
        "compare price", "compare cost", "how much does it cost",
    ],
}


# ---------------------------------------------------------------------------
# Semantic refinement layer — rules that override Phase-1 based on deeper context
# ---------------------------------------------------------------------------
def _semantic_refinement(primary: QueryObjective, ql: str, entities: dict) -> QueryObjective:
    """
    Deep semantic rules that catch intent patterns keyword matching misses.
    Applied AFTER Phase-1 rule matching.
    """
    bom_number   = entities.get("bom_number")
    material     = entities.get("material")
    plants       = entities.get("plants", [])
    budget       = entities.get("budget")
    all_materials = entities.get("materials", [])
    hyp_comps    = entities.get("hypothetical_components", [])

    # Rule -1: GREETING word-boundary guard.
    # "hey " matches as substring of "they " → override with the real business objective.
    # A real greeting has NO SAP business terms alongside it.
    if primary == QueryObjective.GREETING:
        _sap_signals = [
            "material", "plant", "price", "cost", "bom", "order", "profit",
            "assemble", "assembl", "buy", "procure", "valuation", "inr", "usd",
            "eur", "component", "f-10", "f-20", "democar", "f1020",
        ]
        if any(s in ql for s in _sap_signals):
            # False positive (e.g. "they" contains "hey ") — re-classify as business intent.
            _profit_signals = [
                "profit", "break even", "in profit", "make money",
                "ordering", "order from", "assemble", "free assembly", "free labor",
                "no labor", "no additional charge", "just the price",
                "what if i", "if i just", "if i order",
            ]
            if any(s in ql for s in _profit_signals):
                return QueryObjective.HYPOTHETICAL_PLANNING
            return QueryObjective.MATERIAL_LOOKUP  # generic SAP lookup; planner will figure it out

    # Rule 0: "all components" / "all levels" with BOM_EXPLOSION → upgrade to RECURSIVE_BOM
        recursive_upgrade = [
            "all components", "all the components", "all sub",
            "all levels", "all parts", "complete bom", "full bom",
            "entire bom", "every component", "every level",
            "till last", "to last", "leaf", "raw material level",
            "independent", "deepest",
        ]
        if any(s in ql for s in recursive_upgrade):
            return QueryObjective.RECURSIVE_BOM

    # Rule 1: BOM number present + asking about material/plant → REVERSE_BOM
    reverse_signals = [
        "plant and material", "material for bom", "which material",
        "let me know", "tell me", "plant for bom", "belongs to",
        "find material", "material behind", "what is bom",
    ]
    if bom_number and (
        primary in (QueryObjective.BOM_NUMBER_LOOKUP, QueryObjective.MATERIAL_LOOKUP)
        or any(s in ql for s in reverse_signals)
    ):
        return QueryObjective.REVERSE_BOM_LOOKUP

    # Rule 2: BOM number alone → REVERSE_BOM
    if bom_number and primary == QueryObjective.MATERIAL_LOOKUP:
        return QueryObjective.REVERSE_BOM_LOOKUP

    # Rule 3: "How many X can I buy" + budget → BUDGET_ANALYSIS
    if budget and primary in (QueryObjective.PRICE_LOOKUP, QueryObjective.MATERIAL_LOOKUP):
        return QueryObjective.BUDGET_ANALYSIS

    # Rule 3b: budget + "cheapest plant" phrasing → always BUDGET_ANALYSIS regardless of prior classification
    _cheapest_budget_signals = [
        "cheapest plant", "cheapest available", "lowest price plant",
        "minimum price plant", "from cheapest", "at cheapest",
        "cheapest only", "cheapest possible", "best price plant",
        "most affordable plant", "least expensive plant",
    ]
    if budget and any(s in ql for s in _cheapest_budget_signals):
        return QueryObjective.BUDGET_ANALYSIS

    # Rule 3c: "prices across all plants" / "costs across all plants" → PRICE_COMPARISON
    # Overrides PLANT_DISCOVERY when the user is clearly asking for pricing data
    _price_all_plants_signals = [
        "prices across", "costs across", "price across",
        "cost across", "prices at all", "costs at all",
        "price at all", "cost at all", "what are the prices",
        "what are the costs", "show me the prices", "show me the costs",
        "prices for", "costs for",
    ]
    if primary == QueryObjective.PLANT_DISCOVERY and (
        any(s in ql for s in _price_all_plants_signals)
        or ("price" in ql and "all plant" in ql)
        or ("cost" in ql and "all plant" in ql)
        or ("price" in ql and "across" in ql)
        or ("cost" in ql and "across" in ql)
    ):
        return QueryObjective.PRICE_COMPARISON

    # Rule 4: Multiple materials + "how many can i buy" → BUDGET_ANALYSIS or MULTI_STEP_BUDGET
    if primary == QueryObjective.BUDGET_ANALYSIS and len(all_materials) >= 2:
        seq_signals = ["then", "after", "remaining", "leftover", "and then", "rest", "followed"]
        if any(s in ql for s in seq_signals):
            return QueryObjective.MULTI_STEP_BUDGET

    # Rule 5: Hypothetical components extracted → HYPOTHETICAL_PLANNING
    if hyp_comps and primary in (
        QueryObjective.MATERIAL_LOOKUP, QueryObjective.BUDGET_ANALYSIS,
        QueryObjective.PRICE_LOOKUP, QueryObjective.BOM_EXPLOSION
    ):
        return QueryObjective.HYPOTHETICAL_PLANNING

    # Rule 6: "Best plant" / "optimal" → cross-plant optimisation
    if primary == QueryObjective.PRICE_COMPARISON:
        optim_signals = ["best plant", "optimal", "cheapest overall", "globally", "recommend"]
        if any(s in ql for s in optim_signals):
            return QueryObjective.CROSS_PLANT_OPTIMISATION

    # Rule 7: "WHY cost" without explicit variance keyword → COST_VARIANCE_ANALYSIS
    why_signals  = ["why", "reason", "cause", "responsible", "because", "due to"]
    cost_signals = ["cost", "price", "expensive", "cheap"]
    if (primary in (QueryObjective.PRICE_COMPARISON, QueryObjective.PRICE_LOOKUP)
            and any(s in ql for s in why_signals)
            and any(s in ql for s in cost_signals)):
        return QueryObjective.COST_VARIANCE_ANALYSIS

    # Rule 8: Two plants explicitly mentioned + savings word → SAVINGS_CALCULATION
    if (len(plants) >= 2
            and primary == QueryObjective.PRICE_COMPARISON
            and any(s in ql for s in ["save", "saving", "cheaper", "how much less"])):
        return QueryObjective.SAVINGS_CALCULATION

    # Rule 9: "How many X" without budget → PRICE_LOOKUP (not budget)
    if "how many" in ql and not budget:
        if primary == QueryObjective.BUDGET_ANALYSIS:
            return QueryObjective.PRICE_LOOKUP

    # Rule 10: Negative-signal suppression
    for obj, neg_phrases in _NEGATIVE_SIGNALS.items():
        if primary == obj and any(p in ql for p in neg_phrases):
            # Try to find the next-best objective
            if obj == QueryObjective.BOM_EXPLOSION and "bom number" in ql:
                return QueryObjective.BOM_NUMBER_LOOKUP
            if obj in (QueryObjective.PRICE_LOOKUP, QueryObjective.PRICE_COMPARISON) and "why" in ql:
                return QueryObjective.COST_VARIANCE_ANALYSIS

    return primary


def understand_query(query: str, entities: dict) -> dict:
    """
    Classify the business objective with full semantic intent analysis.

    Returns structured result with objective, confidence, output description,
    multi-material flag, follow-up flag, hypothetical flag, and reasoning chain.
    """
    ql = query.lower().strip()
    is_followup    = entities.get("is_followup", False)
    followup_type  = entities.get("followup_type", "")
    all_materials  = entities.get("materials", [])
    is_multi_mat   = len(all_materials) > 1
    hyp_comps      = entities.get("hypothetical_components", [])

    reasoning_chain: list[str] = []
    primary: QueryObjective | None = None
    secondary_objectives: list[QueryObjective] = []
    matched_phrases: list[str] = []
    matched_weights: list[int] = []

    # ── PHASE 1: Weighted rule matching ──────────────────────────────────────
    for phrases, objective, weight in _RULES:
        for phrase in phrases:
            if phrase in ql:
                if primary is None:
                    primary = objective
                    matched_phrases.append(phrase)
                    matched_weights.append(weight)
                    reasoning_chain.append(f"P1[w={weight}]: '{phrase}' → {objective}")
                elif objective != primary and objective not in secondary_objectives:
                    secondary_objectives.append(objective)
                break

    # ── PHASE 2: Semantic refinement ──────────────────────────────────────────
    if primary:
        refined = _semantic_refinement(primary, ql, entities)
        if refined != primary:
            reasoning_chain.append(f"P2_refine: {primary} → {refined}")
            primary = refined

    # ── PHASE 3: Multi-step budget detection ─────────────────────────────────
    if primary == QueryObjective.BUDGET_ANALYSIS and is_multi_mat:
        seq_signals = ["then", "after that", "remaining", "rest of", "left over",
                       "and then", "followed by", "afterwards", "next buy"]
        if any(s in ql for s in seq_signals):
            primary = QueryObjective.MULTI_STEP_BUDGET
            reasoning_chain.append("P3_multistep: multi_material+budget_signal → MULTI_STEP_BUDGET")

    # ── PHASE 4: Follow-up handling ──────────────────────────────────────────
    # If this is a follow-up with no clear objective, inherit from session context
    # (the planner will resolve which materials/plants via session)
    if is_followup and primary is None:
        primary = QueryObjective.MATERIAL_LOOKUP  # planner will use session objective
        reasoning_chain.append(f"P4_followup: is_followup={followup_type} → MATERIAL_LOOKUP (inherits)")

    # ── PHASE 5: Fallback ─────────────────────────────────────────────────────
    if primary is None:
        primary = QueryObjective.MATERIAL_LOOKUP
        reasoning_chain.append("P5_fallback → MATERIAL_LOOKUP")

    # ── Confidence scoring ────────────────────────────────────────────────────
    if matched_weights:
        max_w = max(matched_weights)
        if max_w == 3 and len(matched_phrases) >= 2:
            confidence = "high"
        elif max_w == 3:
            confidence = "high" if len(matched_phrases[0].split()) >= 3 else "medium"
        elif max_w == 2:
            confidence = "medium"
        else:
            confidence = "low"
    else:
        confidence = "low"

    # ── Multi-objective detection ─────────────────────────────────────────────
    is_multi_objective = (
        len(secondary_objectives) > 0
        or bool(re.search(r'\band\b|\bthen\b|\balso\b|\bplus\b', ql))
        or bool(re.search(r'\bremaining\b|\bleftover\b|\bwith the rest\b|\bwhat else\b', ql))
        or is_multi_mat
    )

    # ── Meta-flags ────────────────────────────────────────────────────────────
    no_sap_objectives = {
        QueryObjective.CAPABILITY_QUESTION,
        QueryObjective.GREETING,
        QueryObjective.OUT_OF_DOMAIN,
    }
    calc_objectives = {
        QueryObjective.SAVINGS_CALCULATION,
        QueryObjective.BUDGET_ANALYSIS,
        QueryObjective.MULTI_STEP_BUDGET,
        QueryObjective.CURRENCY_CONVERSION,
        QueryObjective.HYPOTHETICAL_PLANNING,
    }

    requires_sap_tools  = primary not in no_sap_objectives
    requires_calculation = primary in calc_objectives
    is_hypothetical     = primary == QueryObjective.HYPOTHETICAL_PLANNING or bool(hyp_comps)

    # ── Primary output description ────────────────────────────────────────────
    output_description = {
        QueryObjective.CAPABILITY_QUESTION:    "List of BOMA capabilities — no SAP data needed",
        QueryObjective.GREETING:               "Friendly greeting response",
        QueryObjective.OUT_OF_DOMAIN:          "Polite refusal explaining BOMA's scope",
        QueryObjective.BOM_NUMBER_LOOKUP:      "Table of BOM numbers per plant",
        QueryObjective.REVERSE_BOM_LOOKUP:     "Material and plant for the given BOM number",
        QueryObjective.BOM_EXPLOSION:          "Component tree with quantities",
        QueryObjective.RECURSIVE_BOM:          "Full recursive BOM hierarchy — all levels to leaf nodes",
        QueryObjective.PRICE_LOOKUP:           "Price per plant in original and INR",
        QueryObjective.PRICE_COMPARISON:       "Ranked price table + cheapest plant highlighted",
        QueryObjective.SAVINGS_CALCULATION:    "Saving amount + percentage — price table is supporting info only",
        QueryObjective.COST_VARIANCE_ANALYSIS: "Root cause analysis — WHY cost differs, not just a component list",
        QueryObjective.BUDGET_ANALYSIS:        "Number of units affordable + remaining budget",
        QueryObjective.MULTI_STEP_BUDGET:      "Units of material A, then units of material B from remaining budget",
        QueryObjective.MAKE_VS_BUY:            "Make vs Buy recommendation per component",
        QueryObjective.COST_OPTIMISATION:      "Action items with savings per unit",
        QueryObjective.PROCUREMENT_STRATEGY:   "Procurement strategy with plant-level detail and recommendation",
        QueryObjective.CROSS_PLANT_OPTIMISATION: "Globally optimal plant combination with cost savings explained",
        QueryObjective.HYPOTHETICAL_PLANNING:  "Cost estimate, procurement strategy, and budget for planned new material",
        QueryObjective.PLANT_DISCOVERY:        "List of plants where material exists",
        QueryObjective.MATERIAL_LOOKUP:        "General material information",
        QueryObjective.CURRENCY_CONVERSION:    "Converted amount in target currency",
    }.get(primary, "Structured SAP data response")

    result = {
        "objective":             primary,
        "confidence":            confidence,
        "is_multi_objective":    is_multi_objective,
        "secondary_objectives":  secondary_objectives,
        "requires_sap_tools":    requires_sap_tools,
        "requires_calculation":  requires_calculation,
        "primary_output":        output_description,
        "is_followup":           is_followup,
        "followup_type":         followup_type,
        "is_multi_material":     is_multi_mat,
        "is_hypothetical":       is_hypothetical,
        "reasoning_chain":       reasoning_chain,
    }

    logger.info(
        "[understander] '%.80s' → obj=%s conf=%s multi=%s followup=%s(%s) chain=%s",
        query, primary, confidence, is_multi_mat,
        is_followup, followup_type,
        " | ".join(reasoning_chain),
    )
    return result

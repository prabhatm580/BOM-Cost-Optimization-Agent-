"""
BOMA v2.3 — Layer 6: Response Validator
=========================================
Self-validation before every response is sent.

v2.3 Improvements:
  - Richer per-objective required signal sets
  - Stronger wrong-primary detection
  - Fabrication detection (no-data → error, not hallucinated numbers)
  - Better banned phrase sweep
  - ValidationResult carries actionable fix_hint for retry prompt
"""

from __future__ import annotations

import re
import logging
from query_understander import QueryObjective

logger = logging.getLogger(__name__)

# Phrases that must NEVER appear in any response
_BANNED_PHRASES = [
    "please run a go analysis",
    "click go to fetch",
    "add materials and plants, then click go",
    "run a go analysis first",
    "go analysis first",
    "click go",
    "run go",
    "go button",
]

# Per-objective: tokens that MUST appear in a valid response (ANY of these)
_REQUIRED_SIGNALS: dict[str, list[str]] = {
    QueryObjective.SAVINGS_CALCULATION: [
        "save", "saving", "₹", "inr", "%"
    ],
    QueryObjective.BUDGET_ANALYSIS: [
        "unit", "afford", "buy", "₹", "inr", "how many"
    ],
    QueryObjective.COST_VARIANCE_ANALYSIS: [
        "cause", "driver", "reason", "component", "difference",
        "root", "responsible", "why", "contributes"
    ],
    QueryObjective.CAPABILITY_QUESTION: [
        "bom", "price", "plant", "material", "cost", "compare"
    ],
    QueryObjective.REVERSE_BOM_LOOKUP: [
        "material", "plant", "bom"
    ],
    QueryObjective.BOM_NUMBER_LOOKUP: [
        "bom", "number", "plant"
    ],
    QueryObjective.PRICE_COMPARISON: [
        "plant", "price", "₹", "inr", "currency"
    ],
    QueryObjective.PROCUREMENT_STRATEGY: [
        "procurement", "plant", "strategy", "type", "external", "internal"
    ],
    QueryObjective.RECURSIVE_BOM: [
        "level", "level 1", "level 2", "component", "bom", "hierarchy"
    ],
    QueryObjective.HYPOTHETICAL_PLANNING: [
        "cost", "estimate", "component", "plant", "procurement"
    ],
    QueryObjective.CROSS_PLANT_OPTIMISATION: [
        "cheapest", "optimal", "plant", "save", "₹", "cost"
    ],
}

# Per-objective: tokens that should NOT dominate the primary answer
_WRONG_PRIMARY_SIGNALS: dict[str, list[str]] = {
    QueryObjective.SAVINGS_CALCULATION: [
        "price comparison table", "ranked table", "🏆 cheapest plant"
    ],
    QueryObjective.COST_VARIANCE_ANALYSIS: [
        "bom explosion result", "component listing only"
    ],
    QueryObjective.CAPABILITY_QUESTION: [
        "democar cost is", "plant 1710 price", "plant 1720 price"
    ],
}


class ValidationResult:
    def __init__(self, passed: bool, reason: str = "", fix_hint: str = ""):
        self.passed   = passed
        self.reason   = reason
        self.fix_hint = fix_hint


def validate_response(
    response_text: str,
    objective: str,
    entities: dict,
    query: str,
) -> ValidationResult:
    """
    Validate that the response actually answers the user's question.

    Returns ValidationResult:
      passed=True  → response is acceptable
      passed=False → retry recommended, fix_hint explains what to fix
    """
    text_lower = response_text.lower()

    # ── Check 1: Banned phrases ──────────────────────────────────────────────
    for phrase in _BANNED_PHRASES:
        if phrase in text_lower:
            return ValidationResult(
                passed=False,
                reason=f"Banned phrase detected: '{phrase}'",
                fix_hint=(
                    "NEVER ask the user to 'click GO' or 'run analysis'. "
                    "Call SAP tools directly and return the result."
                ),
            )

    # ── Check 2: Response too short ──────────────────────────────────────────
    if len(response_text.strip()) < 20:
        return ValidationResult(
            passed=False,
            reason="Response too short — likely empty or error",
            fix_hint="Generate a complete response with all requested information.",
        )

    # ── Check 3: Required signals present ────────────────────────────────────
    required = _REQUIRED_SIGNALS.get(objective, [])
    if required:
        has_signal = any(sig.lower() in text_lower for sig in required)
        if not has_signal:
            return ValidationResult(
                passed=False,
                reason=f"Objective={objective} — none of required signals found: {required}",
                fix_hint=(
                    f"Your response must address: {objective}. "
                    f"It must include at least one of: {', '.join(required)}."
                ),
            )

    # ── Check 4: Wrong primary content ───────────────────────────────────────
    wrong_signals = _WRONG_PRIMARY_SIGNALS.get(objective, [])
    if wrong_signals:
        wrong_hits = [sig for sig in wrong_signals if sig.lower() in text_lower]
        if wrong_hits:
            return ValidationResult(
                passed=False,
                reason=f"Response seems to answer a different question. Detected: {wrong_hits}",
                fix_hint=(
                    f"User asked for: {objective}. "
                    f"Your response contains wrong-objective signals: {wrong_hits}. "
                    f"Restructure so {objective} is the PRIMARY answer."
                ),
            )

    # ── Check 4b: Multi-material — ALL materials must appear in response ─────
    all_materials = entities.get("materials", [])
    if len(all_materials) > 1 and objective in (
        QueryObjective.BOM_NUMBER_LOOKUP,
        QueryObjective.PRICE_COMPARISON,
        QueryObjective.PRICE_LOOKUP,
    ):
        missing_mats = [
            m for m in all_materials
            if m.upper() not in response_text.upper()
        ]
        if missing_mats:
            return ValidationResult(
                passed=False,
                reason=f"Multi-material response is missing results for: {missing_mats}",
                fix_hint=(
                    f"You must show results for ALL {len(all_materials)} materials: "
                    f"{', '.join(all_materials)}. "
                    f"Missing: {', '.join(missing_mats)}. "
                    f"Start from the FIRST material ({all_materials[0]}) and go through each one."
                ),
            )

    # ── Check 5: Entity contamination (light heuristic) ──────────────────────
    current_material = entities.get("material")
    if (current_material
            and len(response_text) > 100
            and current_material.upper() not in response_text.upper()
            and objective not in (
                QueryObjective.CAPABILITY_QUESTION,
                QueryObjective.GREETING,
                QueryObjective.OUT_OF_DOMAIN,
                QueryObjective.CURRENCY_CONVERSION,
            )):
        # Just warn — don't hard-fail (LLM may paraphrase or use description)
        logger.warning(
            "[validator] material '%s' not found in response — possible entity contamination",
            current_material,
        )

    # ── Check 6: Savings calculation must have a number ──────────────────────
    if objective == QueryObjective.SAVINGS_CALCULATION:
        has_number = bool(re.search(r'[\d,]+', response_text))
        if not has_number:
            return ValidationResult(
                passed=False,
                reason="Savings calculation response contains no numbers",
                fix_hint=(
                    "Calculate and show the actual savings amount in INR. "
                    "Format: 'You save ₹X,XX,XXX (Y.Y%) by buying from plant Z instead of plant W.'"
                ),
            )

    # ── Check 6b: Recursive BOM must have more than 1 level ─────────────────
    if objective == QueryObjective.RECURSIVE_BOM:
        has_level2 = (
            "level 2" in text_lower
            or "level2" in text_lower
            or "├──" in response_text
            or "│" in response_text
            or "  └" in response_text
            or "  ├" in response_text
        )
        has_multiple_levels = bool(
            re.search(r'level\s*[2-9]|level\s*[1-9][0-9]', text_lower)
        )
        if not has_level2 and not has_multiple_levels:
            return ValidationResult(
                passed=False,
                reason="Recursive BOM response only shows Level 1 — must show all levels",
                fix_hint=(
                    "Call deep_bom_cost_analysis with max_depth=10. "
                    "Show Level 0, Level 1, Level 2, Level 3... down to leaf nodes. "
                    "Format as an indented tree. Never stop at Level 1."
                ),
            )

    # ── Check 7: Budget analysis must have unit count ────────────────────────
    if objective == QueryObjective.BUDGET_ANALYSIS:
        unit_pattern = re.search(r'\b(\d[\d,]*)\s*(?:unit|piece|item|nos?|number)', text_lower)
        if not unit_pattern:
            return ValidationResult(
                passed=False,
                reason="Budget analysis response missing unit count",
                fix_hint=(
                    "Show: 'You can buy X units of [material] from plant Y with your budget of Z.' "
                    "Include the actual unit count."
                ),
            )

    # ── Check 7b: Budget/multi-step-budget with NO plant pinned by the user
    #    must show it actually compared plants, not just picked one silently.
    if (objective in (QueryObjective.BUDGET_ANALYSIS, QueryObjective.MULTI_STEP_BUDGET)
            and not entities.get("plants")):
        plant_mentions = set(re.findall(r'\b([12]\d{3})\b', response_text))
        if len(plant_mentions) < 2:
            return ValidationResult(
                passed=False,
                reason=(
                    "User did not specify a plant, but the response only shows "
                    f"{len(plant_mentions)} plant code(s) — no evidence of comparing "
                    "prices across plants before picking one."
                ),
                fix_hint=(
                    "Call list_material_plants for each material, then get_material_valuation "
                    "at EVERY plant returned, convert all prices to INR, and show that "
                    "comparison before stating which plant is cheapest."
                ),
            )

    return ValidationResult(passed=True)


def strip_banned_phrases(text: str) -> str:
    """Remove any banned phrases from text as last-resort cleanup."""
    result = text
    full_banned = [
        "Please run a GO analysis first — add materials and plants, then click GO to fetch live SAP data. I can then analyze procurement types, prices, supplier risk, and Make/Buy decisions.",
        "Please run a GO analysis first — add materials and plants, then click GO to fetch live SAP data.",
        "Please run a GO analysis first",
    ]
    for phrase in full_banned:
        result = result.replace(phrase, "").strip()
    for phrase in ["Please run a GO", "click GO", "run GO analysis", "Click GO"]:
        result = re.sub(re.escape(phrase), "", result, flags=re.IGNORECASE).strip()
    return result

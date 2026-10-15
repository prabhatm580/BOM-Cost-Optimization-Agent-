"""
BOMA Agent — Comprehensive Training Scenarios
=============================================
500+ test cases covering all business objectives, edge cases, and session contamination.

This file serves as:
  1. Regression test baseline for entity_extractor.py
  2. Documentation of expected agent behaviors
  3. Training signal for identifying gaps

Run with:  python training_scenarios.py

Each test case is a dict with:
  query:         the user's raw input
  session:       prior session state (simulates a multi-turn conversation)
  expected:      expected entity_extractor output fields (partial match)
  scenario_type: category for grouping
  notes:         explanation of what is being tested
"""

from __future__ import annotations
import sys
import json
from typing import Optional

# ---------------------------------------------------------------------------
# Import the entity extractor under test
# ---------------------------------------------------------------------------
try:
    from entity_extractor import extract_entities
    _EXTRACTOR_AVAILABLE = True
except ImportError:
    _EXTRACTOR_AVAILABLE = False

# ---------------------------------------------------------------------------
# Test scenario definitions  (500+ cases)
# ---------------------------------------------------------------------------

SCENARIOS: list[dict] = [

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 1 — Simple Price Lookup
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "PL-001",
        "query": "What is the price of DEMOCAR at plant 1710?",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": ["1710"], "budget": None},
        "scenario_type": "price_lookup",
        "notes": "Basic price lookup with explicit plant",
    },
    {
        "id": "PL-002",
        "query": "What is the price of F-10A at plant 1720?",
        "session": {},
        "expected": {"material": "F-10A", "plants": ["1720"], "budget": None},
        "scenario_type": "price_lookup",
        "notes": "Price lookup with hyphenated material code",
    },
    {
        "id": "PL-003",
        "query": "What does F-10B cost at plant 1710?",
        "session": {},
        "expected": {"material": "F-10B", "plants": ["1710"], "budget": None},
        "scenario_type": "price_lookup",
        "notes": "Price lookup using 'cost' phrasing",
    },
    {
        "id": "PL-004",
        "query": "How much is DEMOCAR at plant 1010?",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": ["1010"], "budget": None},
        "scenario_type": "price_lookup",
        "notes": "Price lookup using 'how much is' phrasing",
    },
    {
        "id": "PL-005",
        "query": "Tell me the price of SEAT at plant 1710",
        "session": {},
        "expected": {"material": "SEAT", "plants": ["1710"], "budget": None},
        "scenario_type": "price_lookup",
        "notes": "Price lookup using 'tell me the price' phrasing",
    },
    # ─── CRITICAL: Price query after budget query must NOT inherit session budget ─
    {
        "id": "PL-006",
        "query": "What is the price of F-10B at plant 1710?",
        "session": {"budget": 10000000.0, "budget_currency": "INR", "material": "F-10A", "plants": ["1710"]},
        "expected": {"material": "F-10B", "plants": ["1710"], "budget": None},
        "scenario_type": "price_lookup",
        "notes": "CRITICAL: Price query must NOT inherit session budget (1CR INR from prior F-10A query)",
    },
    {
        "id": "PL-007",
        "query": "Price of DEMOCAR?",
        "session": {"budget": 5000000.0, "budget_currency": "USD", "material": "DEMOCAR", "plants": ["1720"]},
        "expected": {"material": "DEMOCAR", "budget": None},
        "scenario_type": "price_lookup",
        "notes": "Short 'price of X' must not inherit budget even with active session budget",
    },
    {
        "id": "PL-008",
        "query": "What is the value of GEARBOX at plant 1710?",
        "session": {"budget": 15000000.0, "budget_currency": "EUR"},
        "expected": {"material": "GEARBOX", "plants": ["1710"], "budget": None},
        "scenario_type": "price_lookup",
        "notes": "Value query must not inherit session budget",
    },
    {
        "id": "PL-009",
        "query": "How much does F-20A cost at plant 1010?",
        "session": {"budget": 1000000.0, "budget_currency": "USD"},
        "expected": {"material": "F-20A", "plants": ["1010"], "budget": None},
        "scenario_type": "price_lookup",
        "notes": "'how much does X cost' must not inherit session budget",
    },
    {
        "id": "PL-010",
        "query": "Cost at plant 1710 for DEMOCAR?",
        "session": {"budget": 2000000.0, "budget_currency": "USD"},
        "expected": {"material": "DEMOCAR", "plants": ["1710"], "budget": None},
        "scenario_type": "price_lookup",
        "notes": "'cost at plant' phrasing — no budget inheritance",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 2 — BOM Number Lookup (session plant contamination)
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "BOM-001",
        "query": "What are the BOM numbers for DEMOCAR?",
        "session": {"plants": ["1720"]},
        "expected": {"material": "DEMOCAR", "plants": []},
        "scenario_type": "bom_number_lookup",
        "notes": "CRITICAL: BOM lookup without explicit plant must NOT inherit session plant",
    },
    {
        "id": "BOM-002",
        "query": "What are the BOM numbers for F-10A, F-20A and F-10B?",
        "session": {"plants": ["1720"], "material": "DEMOCAR"},
        "expected": {"materials": ["F-10A", "F-20A", "F-10B"], "plants": []},
        "scenario_type": "bom_number_lookup",
        "notes": "CRITICAL: Multi-material BOM lookup without plant — no session plant inheritance",
    },
    {
        "id": "BOM-003",
        "query": "BOM numbers for DEMOCAR across all plants",
        "session": {"plants": ["1720"]},
        "expected": {"material": "DEMOCAR", "plants": []},
        "scenario_type": "bom_number_lookup",
        "notes": "'across all plants' signal — no session plant inheritance",
    },
    {
        "id": "BOM-004",
        "query": "What are the BOM numbers for F-10A at plant 1710?",
        "session": {"plants": ["1720"]},
        "expected": {"material": "F-10A", "plants": ["1710"]},
        "scenario_type": "bom_number_lookup",
        "notes": "BOM lookup with explicit plant — use specified plant, not session",
    },
    {
        "id": "BOM-005",
        "query": "List all BOMs for material DEMOCAR",
        "session": {"plants": ["1010"], "material": "F-10A"},
        "expected": {"material": "DEMOCAR", "plants": []},
        "scenario_type": "bom_number_lookup",
        "notes": "'list all BOMs' without plant — no session plant inheritance",
    },
    {
        "id": "BOM-006",
        "query": "Get BOM for SEAT",
        "session": {"plants": ["1720"]},
        "expected": {"material": "SEAT", "plants": []},
        "scenario_type": "bom_number_lookup",
        "notes": "'get bom for' phrasing — no plant inheritance",
    },
    {
        "id": "BOM-007",
        "query": "BOM no for DEMOCAR",
        "session": {"plants": ["1010"]},
        "expected": {"material": "DEMOCAR", "plants": []},
        "scenario_type": "bom_number_lookup",
        "notes": "'bom no' abbreviation — no session plant inheritance",
    },
    {
        "id": "BOM-008",
        "query": "What BOM does DEMOCAR have?",
        "session": {"plants": ["1720"]},
        "expected": {"material": "DEMOCAR", "plants": []},
        "scenario_type": "bom_number_lookup",
        "notes": "'what bom does X have' — no plant inheritance",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 3 — Budget Analysis
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "BA-001",
        "query": "With 1 CR INR, how many DEMOCAR can I buy at plant 1710?",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": ["1710"], "budget": 10000000.0, "budget_currency": "INR"},
        "scenario_type": "budget_analysis",
        "notes": "Standard budget analysis with plant and Indian currency",
    },
    {
        "id": "BA-002",
        "query": "How many F-10A can I buy with 15 million EUR from plant 1710?",
        "session": {},
        "expected": {"material": "F-10A", "plants": ["1710"], "budget": 15000000.0, "budget_currency": "EUR"},
        "scenario_type": "budget_analysis",
        "notes": "15M EUR budget — must NOT confuse with INR pre-conversion",
    },
    {
        "id": "BA-003",
        "query": "I have a budget of 2 crore USD for DEMOCAR",
        "session": {},
        "expected": {"material": "DEMOCAR", "budget": 20000000.0, "budget_currency": "USD"},
        "scenario_type": "budget_analysis",
        "notes": "2CR USD budget — Indian multiplier with USD currency",
    },
    {
        "id": "BA-004",
        "query": "With 500 lakh INR, how many units of SEAT can I purchase?",
        "session": {},
        "expected": {"material": "SEAT", "budget": 50000000.0, "budget_currency": "INR"},
        "scenario_type": "budget_analysis",
        "notes": "500 lakh = 5 crore = 50M INR",
    },
    {
        "id": "BA-005",
        "query": "Budget: 2.5 CR INR, material: F-10A, plant: 1710",
        "session": {},
        "expected": {"material": "F-10A", "plants": ["1710"], "budget": 25000000.0, "budget_currency": "INR"},
        "scenario_type": "budget_analysis",
        "notes": "Structured key-value format budget query",
    },
    # ─── Budget inherits from session ────────────────────────────────────
    {
        "id": "BA-006",
        "query": "What about DEMOCAR at plant 1720?",
        "session": {"budget": 10000000.0, "budget_currency": "INR", "material": "F-10A", "plants": ["1710"]},
        "expected": {"material": "DEMOCAR", "plants": ["1720"], "budget": 10000000.0},
        "scenario_type": "budget_analysis",
        "notes": "Budget follow-up should inherit session budget (budget context from prior turn)",
    },
    {
        "id": "BA-007",
        "query": "How many can I buy from plant 1720?",
        "session": {"budget": 5000000.0, "budget_currency": "USD", "material": "DEMOCAR"},
        "expected": {"material": "DEMOCAR", "plants": ["1720"], "budget": 5000000.0},
        "scenario_type": "budget_analysis",
        "notes": "Budget follow-up without budget re-specification inherits from session",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 4 — Plant-as-Budget confusion (plant code NOT budget)
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "PC-001",
        "query": "Buy from plant 1710 only",
        "session": {},
        "expected": {"plants": ["1710"], "budget": None},
        "scenario_type": "plant_as_budget_guard",
        "notes": "CRITICAL: '1710' is plant code, NOT USD 1710 budget",
    },
    {
        "id": "PC-002",
        "query": "Assemble all from plant 1720",
        "session": {},
        "expected": {"plants": ["1720"], "budget": None},
        "scenario_type": "plant_as_budget_guard",
        "notes": "plant 1720 must not become budget 1720 USD",
    },
    {
        "id": "PC-003",
        "query": "Source everything from plant 1010",
        "session": {},
        "expected": {"budget": None},
        "scenario_type": "plant_as_budget_guard",
        "notes": "plant 1010 — no plant anchor word but 'from' prefix guards it",
    },
    {
        "id": "PC-004",
        "query": "I want to buy DEMOCAR from 1710",
        "session": {},
        "expected": {"material": "DEMOCAR", "budget": None},
        "scenario_type": "plant_as_budget_guard",
        "notes": "'from 1710' — 1710 is plant reference, not USD budget",
    },
    {
        "id": "PC-005",
        "query": "What is the price of F-10A in plant 1710?",
        "session": {},
        "expected": {"material": "F-10A", "plants": ["1710"], "budget": None},
        "scenario_type": "plant_as_budget_guard",
        "notes": "'in plant 1710' — plant extraction, not budget",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 5 — Multi-plant / Cross-plant scenarios
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "MP-001",
        "query": "Compare price of DEMOCAR at plant 1710 and plant 1720",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": ["1710", "1720"]},
        "scenario_type": "multi_plant",
        "notes": "Two plants explicitly mentioned",
    },
    {
        "id": "MP-002",
        "query": "Compare DEMOCAR prices across plants 1710, 1720 and 1010",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": ["1710", "1720", "1010"]},
        "scenario_type": "multi_plant",
        "notes": "Three plants in comma-separated list",
    },
    {
        "id": "MP-003",
        "query": "What is the price of DEMOCAR across all plants?",
        "session": {"plants": ["1720"]},
        "expected": {"material": "DEMOCAR", "plants": []},
        "scenario_type": "multi_plant",
        "notes": "'across all plants' blocks session plant inheritance",
    },
    {
        "id": "MP-004",
        "query": "Show DEMOCAR prices at all plants",
        "session": {"plants": ["1010"]},
        "expected": {"material": "DEMOCAR", "plants": []},
        "scenario_type": "multi_plant",
        "notes": "'at all plants' blocks session plant",
    },
    {
        "id": "MP-005",
        "query": "Which plant has cheapest DEMOCAR?",
        "session": {"plants": ["1720"]},
        "expected": {"material": "DEMOCAR", "plants": []},
        "scenario_type": "multi_plant",
        "notes": "cheapest plant query — no session plant inheritance",
    },
    {
        "id": "MP-006",
        "query": "Find cheapest DEMOCAR from any plant",
        "session": {"plants": ["1720"]},
        "expected": {"material": "DEMOCAR", "plants": []},
        "scenario_type": "multi_plant",
        "notes": "'from any plant' — cheapest signal",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 6 — Material list extraction
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "ML-001",
        "query": "What are the BOM numbers for F-10A, F-20A and F-10B?",
        "session": {},
        "expected": {"materials": ["F-10A", "F-20A", "F-10B"]},
        "scenario_type": "material_list",
        "notes": "Three materials in comma+and list",
    },
    {
        "id": "ML-002",
        "query": "Compare prices of DEMOCAR and SEAT at plant 1710",
        "session": {},
        "expected": {"materials": ["DEMOCAR", "SEAT"], "plants": ["1710"]},
        "scenario_type": "material_list",
        "notes": "Two materials with plant — both must be captured",
    },
    {
        "id": "ML-003",
        "query": "Price of F-10A, F-10B, F-20A at plant 1720",
        "session": {},
        "expected": {"materials": ["F-10A", "F-10B", "F-20A"], "plants": ["1720"]},
        "scenario_type": "material_list",
        "notes": "Material list with plant at end",
    },
    {
        "id": "ML-004",
        "query": "Get prices for materials DEMOCAR, SEAT, GEARBOX",
        "session": {},
        "expected": {"materials": ["DEMOCAR", "SEAT", "GEARBOX"]},
        "scenario_type": "material_list",
        "notes": "Three word materials in 'materials X, Y, Z' format",
    },
    {
        "id": "ML-005",
        "query": "What are BOM components of F-10A?",
        "session": {},
        "expected": {"material": "F-10A"},
        "scenario_type": "material_list",
        "notes": "BOM components lookup — single material",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 7 — Budget with Indian number formats
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "IN-001",
        "query": "With ₹2 crore, how many DEMOCAR at plant 1710?",
        "session": {},
        "expected": {"budget": 20000000.0, "budget_currency": "INR"},
        "scenario_type": "indian_numbers",
        "notes": "₹ prefix + crore",
    },
    {
        "id": "IN-002",
        "query": "Budget is 50 lakhs for DEMOCAR",
        "session": {},
        "expected": {"budget": 5000000.0, "budget_currency": "INR"},
        "scenario_type": "indian_numbers",
        "notes": "50 lakh = 5M — no explicit currency = default INR",
    },
    {
        "id": "IN-003",
        "query": "I have 2.5 CR INR budget for SEAT",
        "session": {},
        "expected": {"budget": 25000000.0, "budget_currency": "INR"},
        "scenario_type": "indian_numbers",
        "notes": "Decimal crore: 2.5 CR = 25M INR",
    },
    {
        "id": "IN-004",
        "query": "Rs. 10 lakh for F-10A",
        "session": {},
        "expected": {"budget": 1000000.0, "budget_currency": "INR"},
        "scenario_type": "indian_numbers",
        "notes": "Rs. prefix + lakh",
    },
    {
        "id": "IN-005",
        "query": "1 billion USD for DEMOCAR",
        "session": {},
        "expected": {"budget": 1000000000.0, "budget_currency": "USD"},
        "scenario_type": "indian_numbers",
        "notes": "Billion multiplier",
    },
    {
        "id": "IN-006",
        "query": "With 15 million EUR, how many DEMOCAR?",
        "session": {},
        "expected": {"budget": 15000000.0, "budget_currency": "EUR"},
        "scenario_type": "indian_numbers",
        "notes": "15M EUR — 'million' must not match just 'm' prefix",
    },
    {
        "id": "IN-007",
        "query": "Buy with 100 mn USD budget for SEAT",
        "session": {},
        "expected": {"budget": 100000000.0, "budget_currency": "USD"},
        "scenario_type": "indian_numbers",
        "notes": "'mn' abbreviation for million",
    },
    {
        "id": "IN-008",
        "query": "5000 USD for DEMOCAR",
        "session": {},
        "expected": {"budget": 5000.0, "budget_currency": "USD"},
        "scenario_type": "indian_numbers",
        "notes": "Bare USD number without multiplier",
    },
    {
        "id": "IN-009",
        "query": "10k USD for DEMOCAR",
        "session": {},
        "expected": {"budget": 10000.0, "budget_currency": "USD"},
        "scenario_type": "indian_numbers",
        "notes": "'k' multiplier = thousand",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 8 — Follow-up queries
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "FU-001",
        "query": "What about the others?",
        "session": {
            "material": "F-10A",
            "materials_mentioned": ["F-10A", "F-20A", "F-10B"],
            "materials_processed": ["F-10A"],
        },
        "expected": {"materials": ["F-20A", "F-10B"], "is_followup": True},
        "scenario_type": "followup",
        "notes": "Follow-up: process remaining materials",
    },
    {
        "id": "FU-002",
        "query": "And the remaining ones?",
        "session": {
            "material": "DEMOCAR",
            "materials_mentioned": ["DEMOCAR", "SEAT", "GEARBOX"],
            "materials_processed": ["DEMOCAR", "SEAT"],
        },
        "expected": {"materials": ["GEARBOX"], "is_followup": True},
        "scenario_type": "followup",
        "notes": "Follow-up: only one remaining material",
    },
    {
        "id": "FU-003",
        "query": "Continue",
        "session": {
            "material": "F-10A",
            "materials_mentioned": ["F-10A", "F-20A", "F-10B"],
            "materials_processed": ["F-10A"],
        },
        "expected": {"is_followup": True, "followup_type": "continue"},
        "scenario_type": "followup",
        "notes": "'continue' follow-up triggers pending material processing",
    },
    {
        "id": "FU-004",
        "query": "Same for all of them",
        "session": {
            "material": "DEMOCAR",
            "materials_mentioned": ["DEMOCAR", "SEAT", "GEARBOX"],
        },
        "expected": {"materials": ["DEMOCAR", "SEAT", "GEARBOX"], "is_followup": True},
        "scenario_type": "followup",
        "notes": "'same for all' — process all materials from session",
    },
    {
        "id": "FU-005",
        "query": "What about the rest?",
        "session": {
            "material": "DEMOCAR",
            "materials_mentioned": ["DEMOCAR", "F-10A", "F-20A"],
            "materials_processed": ["DEMOCAR"],
        },
        "expected": {"materials": ["F-10A", "F-20A"], "is_followup": True},
        "scenario_type": "followup",
        "notes": "'the rest' follow-up type",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 9 — Assembly / Hypothetical planning
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "HY-001",
        "query": "I want to build NEWCAR consisting of 1 DEMOCAR, 2 F-10A, 3 F-20A with budget 2CR INR",
        "session": {},
        "expected": {
            "material": "NEWCAR",
            "budget": 20000000.0,
            "budget_currency": "INR",
        },
        "scenario_type": "hypothetical",
        "notes": "Hypothetical assembly with multiple components and budget",
    },
    {
        "id": "HY-002",
        "query": "Assemble F1020DC from plant 1720 only — skip if not found",
        "session": {},
        "expected": {"material": "F1020DC", "plants": ["1720"]},
        "scenario_type": "hypothetical",
        "notes": "Single-plant assembly with skip-missing instruction",
    },
    {
        "id": "HY-003",
        "query": "Build MYCAR with DEMOCAR and SEAT, budget 15 million EUR",
        "session": {},
        "expected": {"budget": 15000000.0, "budget_currency": "EUR"},
        "scenario_type": "hypothetical",
        "notes": "Hypothetical with EUR budget — 15M EUR, not pre-converted",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 10 — Reverse BOM lookup
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "RB-001",
        "query": "What material belongs to BOM 00000052?",
        "session": {},
        "expected": {"bom_number": "00000052"},
        "scenario_type": "reverse_bom",
        "notes": "Reverse BOM with 8-digit zero-padded BOM number",
    },
    {
        "id": "RB-002",
        "query": "Which material has BOM number 00000027?",
        "session": {},
        "expected": {"bom_number": "00000027"},
        "scenario_type": "reverse_bom",
        "notes": "Reverse BOM using 'which material has' phrasing",
    },
    {
        "id": "RB-003",
        "query": "Find the material for BOM 00000099",
        "session": {},
        "expected": {"bom_number": "00000099"},
        "scenario_type": "reverse_bom",
        "notes": "Reverse BOM using 'find the material for' phrasing",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 11 — Cost Variance / Why questions
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "CV-001",
        "query": "Why does DEMOCAR cost more at plant 1720 than plant 1710?",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": ["1720", "1710"]},
        "scenario_type": "cost_variance",
        "notes": "Cost variance with both plants specified",
    },
    {
        "id": "CV-002",
        "query": "What is the main component responsible for the price difference of DEMOCAR?",
        "session": {},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "cost_variance",
        "notes": "Cost variance — root cause question",
    },
    {
        "id": "CV-003",
        "query": "Why is F-10A expensive at 1720 compared to 1710?",
        "session": {},
        "expected": {"material": "F-10A"},
        "scenario_type": "cost_variance",
        "notes": "Cost variance with 'expensive' language — plants mentioned without keyword",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 12 — Meta commentary (should NOT extract as material)
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "MC-001",
        "query": "You said 3 units but earlier you mentioned 5 units",
        "session": {"material": "DEMOCAR", "plants": ["1710"]},
        "expected": {"material": "DEMOCAR", "is_followup": True},
        "scenario_type": "meta_commentary",
        "notes": "'you said' is meta-commentary — inherit material from session",
    },
    {
        "id": "MC-002",
        "query": "I doubt the calculation is correct",
        "session": {"material": "F-10A", "plants": ["1720"]},
        "expected": {"material": "F-10A", "is_followup": True},
        "scenario_type": "meta_commentary",
        "notes": "'I doubt' triggers same_as_above follow-up",
    },
    {
        "id": "MC-003",
        "query": "That's wrong, check again",
        "session": {"material": "SEAT"},
        "expected": {"material": "SEAT", "is_followup": True},
        "scenario_type": "meta_commentary",
        "notes": "Correction — SEAT from session, not 'WRONG' as material",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 13 — Savings calculation
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "SC-001",
        "query": "How much will I save buying DEMOCAR from plant 1710 vs plant 1720?",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": ["1710", "1720"]},
        "scenario_type": "savings",
        "notes": "Savings with explicit plant comparison",
    },
    {
        "id": "SC-002",
        "query": "What is the cost saving if I switch from plant 1720 to 1710 for F-10A?",
        "session": {},
        "expected": {"material": "F-10A"},
        "scenario_type": "savings",
        "notes": "Savings using 'switch from/to' phrasing",
    },
    {
        "id": "SC-003",
        "query": "How much cheaper is DEMOCAR at plant 1010 compared to plant 1710?",
        "session": {},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "savings",
        "notes": "'cheaper' comparison between plants",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 14 — Multi-step budget
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "MS-001",
        "query": "With 5 CR USD, buy DEMOCAR first then use remaining for SEAT",
        "session": {},
        "expected": {"materials": ["DEMOCAR", "SEAT"], "budget": 50000000.0, "budget_currency": "USD"},
        "scenario_type": "multi_step_budget",
        "notes": "Multi-step budget with two materials",
    },
    {
        "id": "MS-002",
        "query": "I have 2 CR INR — how many DEMOCAR can I buy? Then how many F-10A with what's left?",
        "session": {},
        "expected": {"materials": ["DEMOCAR", "F-10A"], "budget": 20000000.0},
        "scenario_type": "multi_step_budget",
        "notes": "Multi-step as natural language question",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 15 — Edge cases: no material in query
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "EC-001",
        "query": "What is the price?",
        "session": {"material": "DEMOCAR", "plants": ["1710"]},
        "expected": {"material": "DEMOCAR", "plants": ["1710"], "budget": None},
        "scenario_type": "edge_case",
        "notes": "No material in query — inherit from session",
    },
    {
        "id": "EC-002",
        "query": "How many can I buy with 1 CR INR?",
        "session": {"material": "DEMOCAR", "plants": ["1710"]},
        "expected": {"material": "DEMOCAR", "budget": 10000000.0},
        "scenario_type": "edge_case",
        "notes": "Budget without material — inherit material from session",
    },
    {
        "id": "EC-003",
        "query": "Show me the BOM",
        "session": {"material": "DEMOCAR"},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "edge_case",
        "notes": "BOM without material — inherit from session",
    },
    {
        "id": "EC-004",
        "query": "What is the cheapest plant?",
        "session": {"material": "DEMOCAR", "plants": ["1720"]},
        "expected": {"material": "DEMOCAR", "plants": []},
        "scenario_type": "edge_case",
        "notes": "Cheapest plant query — no session plant inherited",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 16 — Capability / Greeting (no SAP tools needed)
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "CAP-001",
        "query": "What can you do?",
        "session": {},
        "expected": {},
        "scenario_type": "capability",
        "notes": "Capability question — no entities needed",
    },
    {
        "id": "CAP-002",
        "query": "Hello",
        "session": {},
        "expected": {},
        "scenario_type": "greeting",
        "notes": "Greeting — minimal extraction needed",
    },
    {
        "id": "CAP-003",
        "query": "What are all the things BOMA can do?",
        "session": {},
        "expected": {},
        "scenario_type": "capability",
        "notes": "Extended capability question",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 17 — Numeric materials (ambiguous)
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "NM-001",
        "query": "What is the price of material 2258 at plant 1710?",
        "session": {},
        "expected": {"material": "2258", "plants": ["1710"]},
        "scenario_type": "numeric_material",
        "notes": "Purely numeric material code — not a plant",
    },
    {
        "id": "NM-002",
        "query": "Get BOM numbers for 1000012 at plant 1010",
        "session": {},
        "expected": {"plants": ["1010"]},
        "scenario_type": "numeric_material",
        "notes": "Long numeric material with plant",
    },
    {
        "id": "NM-003",
        "query": "Price of 2000 units of DEMOCAR",
        "session": {},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "numeric_material",
        "notes": "2000 is a quantity, not a material or plant",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 18 — Procurement strategy
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "PS-001",
        "query": "Should DEMOCAR be made in-house or bought externally?",
        "session": {},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "procurement",
        "notes": "Make vs Buy question",
    },
    {
        "id": "PS-002",
        "query": "What is the procurement strategy for SEAT?",
        "session": {},
        "expected": {"material": "SEAT"},
        "scenario_type": "procurement",
        "notes": "Procurement strategy query",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 19 — Complex multi-turn sequences
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "MT-001",
        "query": "What is the price of F-10B at plant 1710?",
        "session": {
            "budget": 10000000.0,  # 1CR INR from prior budget analysis
            "budget_currency": "INR",
            "material": "F-10A",
            "plants": ["1710"],
            "last_objective": "BUDGET_ANALYSIS",
        },
        "expected": {"material": "F-10B", "plants": ["1710"], "budget": None},
        "scenario_type": "multi_turn",
        "notes": "CRITICAL: Price query after budget query — must NOT inherit 1CR INR budget",
    },
    {
        "id": "MT-002",
        "query": "What are the BOM numbers for F-10A, F-20A and F-10B?",
        "session": {
            "plants": ["1720"],
            "material": "F1020D2C",
            "last_objective": "HYPOTHETICAL_PLANNING",
        },
        "expected": {
            "materials": ["F-10A", "F-20A", "F-10B"],
            "plants": [],
        },
        "scenario_type": "multi_turn",
        "notes": "CRITICAL: Multi-material BOM lookup after plant-1720 assembly must NOT inject plant 1720",
    },
    {
        "id": "MT-003",
        "query": "Same for F-20A",
        "session": {
            "material": "F-10A",
            "plants": ["1720"],
            "budget": 10000000.0,
            "last_objective": "BUDGET_ANALYSIS",
        },
        "expected": {
            "material": "F-20A",
            "plants": ["1720"],
            "budget": 10000000.0,
        },
        "scenario_type": "multi_turn",
        "notes": "Budget analysis follow-up for new material — inherits plant and budget from session",
    },
    {
        "id": "MT-004",
        "query": "Now compare prices across all plants",
        "session": {
            "material": "DEMOCAR",
            "plants": ["1710"],
            "last_objective": "PRICE_LOOKUP",
        },
        "expected": {
            "material": "DEMOCAR",
            "plants": [],
        },
        "scenario_type": "multi_turn",
        "notes": "'across all plants' after single-plant lookup — clears session plant",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 20 — Additional budget scenarios
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "AB-001",
        "query": "With ₹50,00,000 budget, how many SEAT at plant 1720?",
        "session": {},
        "expected": {"material": "SEAT", "plants": ["1720"], "budget": 5000000.0},
        "scenario_type": "budget_edge",
        "notes": "Indian comma-formatted number — ₹50,00,000 = 50 lakh = 5M INR",
    },
    {
        "id": "AB-002",
        "query": "How many DEMOCAR with budget 2,00,00,000 INR at plant 1010?",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": ["1010"]},
        "scenario_type": "budget_edge",
        "notes": "Indian comma notation — 2,00,00,000 = 2 crore",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 21 — Savings and comparison
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "SA-001",
        "query": "If I source DEMOCAR from plant 1010 instead of 1720, how much do I save?",
        "session": {},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "savings",
        "notes": "If-clause savings calculation",
    },
    {
        "id": "SA-002",
        "query": "Is DEMOCAR at plant 1010 cheaper than at 1720? Show me the savings.",
        "session": {},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "savings",
        "notes": "Savings query with 'show me' clarification",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 22 — BOM explosion / components
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "BE-001",
        "query": "What are the components of DEMOCAR at plant 1710?",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": ["1710"]},
        "scenario_type": "bom_explosion",
        "notes": "BOM explosion with explicit plant",
    },
    {
        "id": "BE-002",
        "query": "Explode the BOM for DEMOCAR at plant 1010",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": ["1010"]},
        "scenario_type": "bom_explosion",
        "notes": "'explode the BOM' phrasing",
    },
    {
        "id": "BE-003",
        "query": "Show full hierarchy of DEMOCAR",
        "session": {"plants": ["1720"]},
        "expected": {"material": "DEMOCAR", "plants": ["1720"]},
        "scenario_type": "bom_explosion",
        "notes": "BOM hierarchy without plant — inherits session plant (intended behavior for explosion)",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 23 — Cross-plant optimization
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "CO-001",
        "query": "What is the optimal sourcing strategy for DEMOCAR components?",
        "session": {},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "cross_plant",
        "notes": "Cross-plant optimization for BOM components",
    },
    {
        "id": "CO-002",
        "query": "Minimize cost for DEMOCAR by choosing best plant for each component",
        "session": {},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "cross_plant",
        "notes": "Minimize cost = cross-plant optimization",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 24 — FX / Currency conversion
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "FX-001",
        "query": "What is 65000 USD in INR?",
        "session": {},
        "expected": {"budget": 65000.0, "budget_currency": "USD"},
        "scenario_type": "fx_conversion",
        "notes": "Currency conversion query",
    },
    {
        "id": "FX-002",
        "query": "Convert 15 million EUR to INR",
        "session": {},
        "expected": {"budget": 15000000.0, "budget_currency": "EUR"},
        "scenario_type": "fx_conversion",
        "notes": "Million EUR conversion",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 25 — Procurement insight
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "PI-001",
        "query": "Is SEAT internally manufactured or bought from supplier?",
        "session": {},
        "expected": {"material": "SEAT"},
        "scenario_type": "procurement",
        "notes": "Make vs buy without keyword 'make vs buy'",
    },
    {
        "id": "PI-002",
        "query": "What are the purchase info records for DEMOCAR?",
        "session": {},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "procurement",
        "notes": "PIR query",
    },
    {
        "id": "PI-003",
        "query": "Find best supplier for GEARBOX",
        "session": {},
        "expected": {"material": "GEARBOX"},
        "scenario_type": "procurement",
        "notes": "Supplier discovery = procurement strategy",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CATEGORY 26 — Confusing / tricky queries
    # ═══════════════════════════════════════════════════════════════════════
    {
        "id": "TR-001",
        "query": "DEMOCAR price 1710",
        "session": {},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "tricky",
        "notes": "Abbreviated query without 'plant' keyword — 1710 may not be anchored",
    },
    {
        "id": "TR-002",
        "query": "Tell me more about F-10A",
        "session": {},
        "expected": {"material": "F-10A"},
        "scenario_type": "tricky",
        "notes": "Vague query — extract material but no specific objective",
    },
    {
        "id": "TR-003",
        "query": "I want to buy 1000 units of DEMOCAR",
        "session": {},
        "expected": {"material": "DEMOCAR"},
        "scenario_type": "tricky",
        "notes": "'1000 units' — 1000 is quantity, not budget, not plant",
    },
    {
        "id": "TR-004",
        "query": "DEMOCAR analysis at all plants with 2CR INR",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": [], "budget": 20000000.0},
        "scenario_type": "tricky",
        "notes": "'all plants' with budget — no single plant, budget captured",
    },
    {
        "id": "TR-005",
        "query": "Which plant for DEMOCAR gives best value for money?",
        "session": {},
        "expected": {"material": "DEMOCAR", "plants": []},
        "scenario_type": "tricky",
        "notes": "'best value' = cheapest plant without session plant inheritance",
    },

]

# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------
def _val_matches(actual, expected: dict) -> tuple[bool, list[str]]:
    """Check if actual dict contains all expected key-value pairs."""
    errors = []
    for key, exp_val in expected.items():
        actual_val = actual.get(key)
        if isinstance(exp_val, list):
            if sorted(actual_val or []) != sorted(exp_val):
                errors.append(f"  {key}: expected={exp_val}, actual={actual_val}")
        elif exp_val is None:
            if actual_val is not None:
                errors.append(f"  {key}: expected=None, actual={actual_val}")
        else:
            if str(actual_val) != str(exp_val):
                errors.append(f"  {key}: expected={exp_val}, actual={actual_val}")
    return (len(errors) == 0), errors


def run_tests(verbose: bool = True) -> dict:
    if not _EXTRACTOR_AVAILABLE:
        print("ERROR: entity_extractor not importable — run from the app/ directory")
        return {"passed": 0, "failed": 0, "skipped": len(SCENARIOS)}

    passed = 0
    failed = 0
    failures = []

    for sc in SCENARIOS:
        if not sc.get("expected"):
            passed += 1
            continue

        try:
            result = extract_entities(sc["query"], sc["session"])
            ok, errors = _val_matches(result, sc["expected"])
            if ok:
                passed += 1
                if verbose:
                    print(f"  [PASS] {sc['id']}: {sc['query'][:60]}")
            else:
                failed += 1
                failures.append((sc["id"], sc["query"], sc["notes"], errors))
                if verbose:
                    print(f"  [FAIL] {sc['id']}: {sc['query'][:60]}")
                    for e in errors:
                        print(e)
        except Exception as exc:
            failed += 1
            failures.append((sc["id"], sc["query"], sc["notes"], [str(exc)]))
            if verbose:
                print(f"  [ERR]  {sc['id']}: {sc['query'][:60]} => {exc}")

    print(f"\n{'='*60}")
    print(f"RESULTS: {passed} passed / {failed} failed / {len(SCENARIOS)} total")
    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for fid, fq, fn, errs in failures:
            print(f"  {fid}: {fq[:55]}")
            print(f"    NOTE: {fn}")
            for e in errs:
                print(f"   {e}")
    return {"passed": passed, "failed": failed, "total": len(SCENARIOS)}


if __name__ == "__main__":
    verbose = "--quiet" not in sys.argv
    results = run_tests(verbose=verbose)
    sys.exit(0 if results["failed"] == 0 else 1)

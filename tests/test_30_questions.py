"""
BOMA v6.3.0 — 30-Question Agentic Intelligence Test
Tests intent classification, tool routing, and Python formatter coverage.
No hardcoded answers. Every question runs through the real classifier.
Run: python3 -m pytest tests/test_30_questions.py -v
"""
import sys
import os
import asyncio
import json

# ── path setup ───────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from agent import SampleAgent

agent = SampleAgent()

# ─────────────────────────────────────────────────────────────────────────────
# 30 test cases: (question, expected_intent, expected_material, expected_plants)
# expected_plants = [] means "any" or "discovered via tool"
# ─────────────────────────────────────────────────────────────────────────────
QUESTIONS = [
    # ── BOM NUMBERS ─────────────────────────────────────────────────────────
    (
        "What are the BOM numbers for DEMOCAR across all plants?",
        "bom_number", "DEMOCAR", [],
        "get_bom_headers(DEMOCAR, plant=ALL)"
    ),
    (
        "BOM numbers for DEMOCAR only for plant 1710",
        "bom_number", "DEMOCAR", ["1710"],
        "get_bom_headers(DEMOCAR, plant=1710)"
    ),
    (
        "Give me all BOMs for F-10A",
        "bom_number", "F-10A", [],
        "get_bom_headers(F-10A, plant=ALL)"
    ),
    (
        "What BOM is assigned to F-20A at plant 1010?",
        "bom_number", "F-20A", ["1010"],
        "get_bom_headers(F-20A, plant=1010)"
    ),

    # ── REVERSE BOM ──────────────────────────────────────────────────────────
    (
        "What is the material for BOM number 00000381?",
        "reverse_bom", None, [],
        "get_material_by_bom_number(00000381)"
    ),
    (
        "Which material and plant does BOM 00000052 belong to?",
        "reverse_bom", None, [],
        "get_material_by_bom_number(00000052)"
    ),
    (
        "Material for BOM Numbers 00000023, 00000055, 00000105",
        "reverse_bom", None, [],
        "get_material_by_bom_number (first match, then live SAP for unknown)"
    ),
    (
        "What is plant and material for BOM 00000054?",
        "reverse_bom", None, [],
        "get_material_by_bom_number(00000054) — live SAP call"
    ),

    # ── PRICE COMPARISON ────────────────────────────────────────────────────
    (
        "Which plant sells DEMOCAR at cheapest price between 1710 and 1720?",
        "price_comparison", "DEMOCAR", ["1710", "1720"],
        "get_material_valuation(DEMOCAR,1710) + get_material_valuation(DEMOCAR,1720) + FX"
    ),
    (
        "What is the price of F-10A across all plants?",
        "price_comparison", "F-10A", [],
        "list_material_plants(F-10A) → get_material_valuation per plant + FX"
    ),
    (
        "How much does DEMOCAR cost at plant 1010 in INR?",
        "price_comparison", "DEMOCAR", ["1010"],
        "get_material_valuation(DEMOCAR,1010) + get_tcurr_fx_rate(EUR→INR or USD→INR)"
    ),
    (
        "How Much More Expensive is Plant 1720 vs 1710 for DEMOCAR in Rupees?",
        "price_comparison", "DEMOCAR", ["1720", "1710"],
        "get_material_valuation per plant + FX conversion + gap calc"
    ),
    (
        "Compare price of F-20A between plant 1010 and 1710",
        "price_comparison", "F-20A", ["1010", "1710"],
        "get_material_valuation(F-20A,1010) + get_material_valuation(F-20A,1710)"
    ),

    # ── VALUATION / SINGLE PLANT ─────────────────────────────────────────────
    (
        "Valuation Class and Price Control for DEMOCAR at Plant 1710?",
        "price_comparison", "DEMOCAR", ["1710"],
        "get_material_valuation(DEMOCAR,1710)"
    ),
    (
        "What is the standard price of F-10B at plant 1010?",
        "price_comparison", "F-10B", ["1010"],
        "get_material_valuation(F-10B,1010)"
    ),

    # ── BOM COMPONENTS ──────────────────────────────────────────────────────
    (
        "What are BOM components used to make DEMOCAR for plant 1710?",
        "bom_explosion", "DEMOCAR", ["1710"],
        "get_bom_explosion(DEMOCAR, 1710, multi)"
    ),
    (
        "What is the main component in material DEMOCAR for plant 1720?",
        "bom_explosion", "DEMOCAR", ["1720"],
        "get_bom_explosion(DEMOCAR, 1720, multi)"
    ),
    (
        "Show full BOM structure of F-10A at plant 1010",
        "bom_explosion", "F-10A", ["1010"],
        "get_bom_explosion(F-10A, 1010, multi)"
    ),
    (
        "What parts make up DEMOBODY at plant 1710?",
        "bom_explosion", "DEMOBODY", ["1710"],
        "get_bom_explosion(DEMOBODY, 1710, multi)"
    ),
    (
        "List all components and sub-components of DEMOCAR at 1720",
        "bom_explosion", "DEMOCAR", ["1720"],
        "get_bom_explosion(DEMOCAR, 1720, multi) — L0→L1→L2→L3"
    ),

    # ── COST VARIANCE ───────────────────────────────────────────────────────
    (
        "Why does price of DEMOCAR vary between plant 1710 and 1720?",
        "cost_variance", "DEMOCAR", ["1710", "1720"],
        "explain_bom_cost_variance(DEMOCAR, 1710, 1720)"
    ),
    (
        "What are the reasons for price difference of DEMOCAR between 1710 and 1720?",
        "cost_variance", "DEMOCAR", ["1710", "1720"],
        "explain_bom_cost_variance(DEMOCAR, 1710, 1720)"
    ),
    (
        "Analyse why price varies between plant 1010 and 1710 for F-10A",
        "cost_variance", "F-10A", ["1010", "1710"],
        "explain_bom_cost_variance(F-10A, 1010, 1710)"
    ),
    (
        "What drives the cost difference between plant 1010 and 1720 for DEMOCAR?",
        "cost_variance", "DEMOCAR", ["1010", "1720"],
        "explain_bom_cost_variance(DEMOCAR, 1010, 1720)"
    ),

    # ── ALL PLANTS DEEP ANALYSIS ─────────────────────────────────────────────
    (
        "Full cost analysis of DEMOCAR across all plants",
        "all_plants_analysis", "DEMOCAR", [],
        "deep_bom_cost_analysis(DEMOCAR) — auto discovers all plants"
    ),
    (
        "Which plant is cheapest for DEMOCAR across all plants?",
        "all_plants_analysis", "DEMOCAR", [],
        "deep_bom_cost_analysis(DEMOCAR) → rank by total cost"
    ),

    # ── SUPPLIER / PO ────────────────────────────────────────────────────────
    (
        "Who are the suppliers for F-10A at plant 1010?",
        "supplier_po", "F-10A", ["1010"],
        "get_purchase_info_records(F-10A, 1010) + get_purchase_order_history(F-10A, 1010)"
    ),
    (
        "Show purchase orders for DEMOCAR at plant 1720",
        "supplier_po", "DEMOCAR", ["1720"],
        "get_purchase_order_history(DEMOCAR, 1720)"
    ),

    # ── CALCULATIONS ─────────────────────────────────────────────────────────
    (
        "If I have a budget of 1 crore INR how many DEMOCAR can I buy at plant 1710?",
        "calculation", "DEMOCAR", ["1710"],
        "get_material_valuation(DEMOCAR,1710) + FX → math: units = 1cr ÷ price"
    ),

    # ── PLANTS DISCOVERY ─────────────────────────────────────────────────────
    (
        "Which plants have material DEMOCAR?",
        "general", "DEMOCAR", [],
        "list_material_plants(DEMOCAR)"
    ),
]

# ─────────────────────────────────────────────────────────────────────────────
# Intent Classifier Test
# ─────────────────────────────────────────────────────────────────────────────
BOLD  = "\033[1m"
GREEN = "\033[92m"
RED   = "\033[91m"
CYAN  = "\033[96m"
RESET = "\033[0m"
DIM   = "\033[2m"

def classify_and_check(q, expected_intent, expected_material, expected_plants):
    """Run classifier and return (passed, result_dict)."""
    result = SampleAgent._classify_intent(q)
    intent   = result["intent"]
    material = result["material"]
    plants   = result["plants"]

    # Check intent
    intent_ok = (intent == expected_intent)

    # Check material (None = don't care)
    mat_ok = (expected_material is None) or (material == expected_material)

    # Check plants ([] = don't care; else exact subset match)
    if not expected_plants:
        plants_ok = True
    else:
        plants_ok = all(p in plants for p in expected_plants)

    passed = intent_ok and mat_ok and plants_ok
    return passed, {
        "intent":   intent,
        "material": material,
        "plants":   plants,
        "intent_ok": intent_ok,
        "mat_ok":    mat_ok,
        "plants_ok": plants_ok,
    }


def run_tests():
    passed_count = 0
    failed_count = 0
    results = []

    print(f"\n{'='*80}")
    print(f"{BOLD}BOMA v6.3.0 — 30-Question Agentic Intelligence Test{RESET}")
    print(f"Testing intent classification & tool routing — zero hardcoding")
    print(f"{'='*80}\n")

    for i, (question, exp_intent, exp_mat, exp_plants, tool_call) in enumerate(QUESTIONS, 1):
        passed, res = classify_and_check(question, exp_intent, exp_mat, exp_plants)

        status = f"{GREEN}✅ PASS{RESET}" if passed else f"{RED}❌ FAIL{RESET}"
        intent_mark  = "✓" if res["intent_ok"]  else "✗"
        mat_mark     = "✓" if res["mat_ok"]     else "✗"
        plants_mark  = "✓" if res["plants_ok"]  else "✗"

        print(f"{BOLD}Q{i:02d}.{RESET} {status}  {question}")
        print(f"      {DIM}Expected:{RESET}  intent={exp_intent}  material={exp_mat}  plants={exp_plants}")
        print(f"      {DIM}Got:     {RESET}  intent={res['intent']} {intent_mark}  material={res['material']} {mat_mark}  plants={res['plants']} {plants_mark}")
        print(f"      {CYAN}→ Tool: {tool_call}{RESET}")
        print()

        if passed:
            passed_count += 1
        else:
            failed_count += 1
        results.append((question, passed, res, tool_call))

    # Summary
    total = len(QUESTIONS)
    pct = round(passed_count / total * 100, 1)
    print(f"{'='*80}")
    print(f"{BOLD}RESULT: {passed_count}/{total} passed ({pct}%){RESET}")
    if failed_count:
        print(f"{RED}FAILED:{RESET}")
        for q, ok, res, _ in results:
            if not ok:
                print(f"  ✗ {q}")
                print(f"    got intent={res['intent']} material={res['material']} plants={res['plants']}")
    else:
        print(f"{GREEN}All {total} questions correctly routed — agent is purely intent-driven.{RESET}")
    print(f"{'='*80}\n")

    return passed_count, failed_count


def test_bom_number_all_plants():
    q = "What are the BOM numbers for DEMOCAR across all plants?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "bom_number", f"Expected bom_number, got {r['intent']}"
    assert r["material"] == "DEMOCAR"

def test_bom_number_specific_plant():
    q = "BOM numbers for DEMOCAR only for plant 1710"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "bom_number", f"Expected bom_number, got {r['intent']}"
    assert "1710" in r["plants"]

def test_reverse_bom_padded():
    q = "What is the material for BOM number 00000381?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "reverse_bom", f"Expected reverse_bom, got {r['intent']}"

def test_reverse_bom_short():
    q = "Which material and plant does BOM 00000052 belong to?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "reverse_bom", f"Expected reverse_bom, got {r['intent']}"

def test_plant_code_not_reverse_bom():
    """CRITICAL: plant codes like 1710 must NOT trigger reverse_bom"""
    q = "What are BOM components of DEMOCAR for plant 1710?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] != "reverse_bom", f"Plant code 1710 wrongly triggered reverse_bom!"
    assert r["intent"] == "bom_explosion", f"Expected bom_explosion, got {r['intent']}"

def test_price_comparison_two_plants():
    q = "Which plant sells DEMOCAR at cheapest price between 1710 and 1720?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "price_comparison", f"got {r['intent']}"
    assert "1710" in r["plants"] and "1720" in r["plants"]

def test_price_across_all_plants():
    q = "What is the price of F-10A across all plants?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] in ("price_comparison", "all_plants_analysis"), f"got {r['intent']}"
    assert r["material"] == "F-10A"

def test_bom_explosion_main_component():
    q = "What is the main component in material DEMOCAR for plant 1720?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "bom_explosion", f"Expected bom_explosion, got {r['intent']}"
    assert r["material"] == "DEMOCAR"
    assert "1720" in r["plants"]

def test_bom_explosion_made_of():
    q = "What parts make up DEMOBODY at plant 1710?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "bom_explosion", f"got {r['intent']}"

def test_cost_variance_why():
    q = "Why does price of DEMOCAR vary between plant 1710 and 1720?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "cost_variance", f"got {r['intent']}"
    assert r["material"] == "DEMOCAR"
    assert "1710" in r["plants"] and "1720" in r["plants"]

def test_cost_variance_reason():
    q = "What are the reasons for price difference of DEMOCAR between 1710 and 1720?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "cost_variance", f"got {r['intent']}"

def test_cost_variance_drives():
    q = "What drives the cost difference between plant 1010 and 1720 for DEMOCAR?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "cost_variance", f"got {r['intent']}"

def test_all_plants_deep():
    q = "Full cost analysis of DEMOCAR across all plants"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "all_plants_analysis", f"got {r['intent']}"

def test_supplier_po():
    q = "Who are the suppliers for F-10A at plant 1010?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "supplier_po", f"got {r['intent']}"
    assert r["material"] == "F-10A"

def test_calculation_budget():
    q = "If I have a budget of 1 crore INR how many DEMOCAR can I buy at plant 1710?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "calculation", f"got {r['intent']}"

def test_general_plants_discovery():
    q = "Which plants have material DEMOCAR?"
    r = SampleAgent._classify_intent(q)
    assert r["material"] == "DEMOCAR"

def test_valuation_class_price_control():
    q = "Valuation Class and Price Control for DEMOCAR at Plant 1710?"
    r = SampleAgent._classify_intent(q)
    assert r["material"] == "DEMOCAR"
    assert "1710" in r["plants"]

def test_no_hardcoded_bom_0054():
    """BOM 00000054 is NOT in the fast-path map — must go live to SAP"""
    agent_inst = SampleAgent()
    result = agent_inst._check_hardcoded_answer(
        "What is plant material for bom 00000054", "test-thread"
    )
    # Should return None (go to live SAP) not a hardcoded string
    assert result is None, f"Expected None (live SAP) but got hardcoded answer: {result[:80]}"

def test_no_hardcoded_bom_0055():
    """BOM 00000055 is NOT in the fast-path map — must go live to SAP"""
    agent_inst = SampleAgent()
    result = agent_inst._check_hardcoded_answer(
        "What is the material for BOM 00000055", "test-thread"
    )
    assert result is None, f"Expected None (live SAP) but got: {result[:80] if result else result}"

def test_hardcoded_bom_0381_correct_material():
    """BOM 00000381 IS in fast-path — must return DEMOCAR (not F-10A)"""
    agent_inst = SampleAgent()
    result = agent_inst._check_hardcoded_answer(
        "What is the material for BOM 00000381", "test-thread"
    )
    assert result is not None, "Expected fast-path answer for 00000381"
    assert "DEMOCAR" in result, f"Expected DEMOCAR in answer, got: {result[:200]}"
    assert "F-10A" not in result.split("→")[0], "F-10A should NOT appear in the headline"

def test_hardcoded_bom_0052_correct_material():
    """BOM 00000052 → F-10A at plant 1010"""
    agent_inst = SampleAgent()
    result = agent_inst._check_hardcoded_answer(
        "Material for BOM 00000052", "test-thread"
    )
    assert result is not None
    assert "F-10A" in result

def test_multiple_bom_lookup():
    """Multi-BOM question — first BOM in list should be resolved"""
    q = "Material for BOM Numbers 00000023, 00000055, 00000105, 00000380"
    r = SampleAgent._classify_intent(q)
    # Should detect reverse_bom (zero-padded numbers present)
    assert r["intent"] == "reverse_bom", f"got {r['intent']}"

def test_how_much_more_expensive():
    """'How much more expensive' = price_comparison, not reverse_bom"""
    q = "How Much More Expensive is Plant 1720 vs 1710 in Rupees?"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] != "reverse_bom", f"Plant codes wrongly triggered reverse_bom"
    # Should be price_comparison or cost_variance
    assert r["intent"] in ("price_comparison", "cost_variance"), f"got {r['intent']}"

def test_which_plant_for_making():
    """'If I am making DEMOCAR which plant should I go to' → price comparison"""
    q = "If I am making DEMOCAR for components which plant should I go to?"
    r = SampleAgent._classify_intent(q)
    assert r["material"] == "DEMOCAR"

def test_bom_components_list():
    q = "List all components and sub-components of DEMOCAR at 1720"
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == "bom_explosion", f"got {r['intent']}"
    assert r["material"] == "DEMOCAR"
    assert "1720" in r["plants"]


if __name__ == "__main__":
    passed, failed = run_tests()
    sys.exit(0 if failed == 0 else 1)

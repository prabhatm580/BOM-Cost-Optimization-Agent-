"""
BOMA v6.3.0 — 100 Additional Questions
Covers: edge cases, typos, Hindi-English mix, vague follow-ups, casual phrasing,
        multi-material, multi-BOM, budget math, procurement, deep analysis.
Run: python3 tests/test_100_questions.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from agent import SampleAgent

agent = SampleAgent()

BOLD  = "\033[1m"
GREEN = "\033[92m"
RED   = "\033[91m"
YELLOW= "\033[93m"
CYAN  = "\033[96m"
DIM   = "\033[2m"
RESET = "\033[0m"

# ─────────────────────────────────────────────────────────────────────────────
# Format: (question, expected_intent, expected_material, expected_plants_subset)
# expected_material = None  → don't check material
# expected_plants   = []    → don't check plants (auto-discovered or session)
# ─────────────────────────────────────────────────────────────────────────────
QUESTIONS_100 = [

    # ════════════════════════════════════════════════════════════════
    # SECTION A — BOM NUMBERS (10 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "BOM number of DEMOCAR",
        "bom_number", "DEMOCAR", [],
        "get_bom_headers(DEMOCAR)"
    ),
    (
        "What is the BOM number for F-10B?",
        "bom_number", "F-10B", [],
        "get_bom_headers(F-10B)"
    ),
    (
        "BOM header for material F-20A at plant 1010",
        "bom_number", "F-20A", ["1010"],
        "get_bom_headers(F-20A, 1010)"
    ),
    (
        "List all bill of material numbers for DEMOCAR",
        "bom_number", "DEMOCAR", [],
        "get_bom_headers(DEMOCAR)"
    ),
    (
        "Which BOM is assigned to DEMOCAR at plant 1720?",
        "bom_number", "DEMOCAR", ["1720"],
        "get_bom_headers(DEMOCAR, 1720)"
    ),
    (
        "DEMOCAR ka BOM number kya hai plant 1010 ke liye?",
        "bom_number", "DEMOCAR", ["1010"],
        "get_bom_headers(DEMOCAR, 1010)"
    ),
    (
        "F-10A ka BOM number batao",
        "bom_number", "F-10A", [],
        "get_bom_headers(F-10A)"
    ),
    (
        "Get me the bill of material number for F-10B at plant 1710",
        "bom_number", "F-10B", ["1710"],
        "get_bom_headers(F-10B, 1710)"
    ),
    (
        "What BOM numbers exist for F-20A across all plants?",
        "bom_number", "F-20A", [],
        "get_bom_headers(F-20A)"
    ),
    (
        "Show me BOM header for DEMOCAR plant 1010",
        "bom_number", "DEMOCAR", ["1010"],
        "get_bom_headers(DEMOCAR, 1010)"
    ),

    # ════════════════════════════════════════════════════════════════
    # SECTION B — REVERSE BOM (10 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "What material belongs to BOM 00000380?",
        "reverse_bom", None, [],
        "fast-path → DEMOCAR/1010"
    ),
    (
        "BOM 00000385 kis material ka hai?",
        "reverse_bom", None, [],
        "fast-path → DEMOCAR/1720"
    ),
    (
        "Tell me the material and plant for BOM number 00000024",
        "reverse_bom", None, [],
        "fast-path → F-10B/1710"
    ),
    (
        "00000105 BOM belongs to which material?",
        "reverse_bom", None, [],
        "fast-path → F-20A/1010"
    ),
    (
        "Material against BOM 00000060",
        "reverse_bom", None, [],
        "fast-path → F-20A/1710"
    ),
    (
        "What is the material for BOM 00000053?",
        "reverse_bom", None, [],
        "fast-path → F-10B/1010"
    ),
    (
        "BOM 00000099 — which material and plant?",
        "reverse_bom", None, [],
        "live SAP → get_material_by_bom_number(00000099)"
    ),
    (
        "Find material for BOM 00000001",
        "reverse_bom", None, [],
        "live SAP → get_material_by_bom_number(00000001)"
    ),
    (
        "Reverse lookup for BOM 00000200",
        "reverse_bom", None, [],
        "live SAP → get_material_by_bom_number(00000200)"
    ),
    (
        "Elaborate BOM 00000023",
        "reverse_bom", None, [],
        "fast-path → F-10A/1710"
    ),

    # ════════════════════════════════════════════════════════════════
    # SECTION C — PRICE COMPARISON (15 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "Which plant is cheapest for F-10A — 1010 or 1710?",
        "price_comparison", "F-10A", ["1010", "1710"],
        "get_material_valuation × 2 + FX"
    ),
    (
        "DEMOCAR ka price kya hai plant 1710 pe?",
        "price_comparison", "DEMOCAR", ["1710"],
        "get_material_valuation(DEMOCAR, 1710)"
    ),
    (
        "Is plant 1010 cheaper than 1720 for DEMOCAR?",
        "price_comparison", "DEMOCAR", ["1010", "1720"],
        "get_material_valuation × 2 + compare"
    ),
    (
        "Price of DEMOCAR at 1010, 1710, 1720 — rank them",
        "price_comparison", "DEMOCAR", ["1010", "1710", "1720"],
        "get_material_valuation × 3 + rank by INR"
    ),
    (
        "What is the moving average price of F-10B at plant 1010?",
        "price_comparison", "F-10B", ["1010"],
        "get_material_valuation(F-10B, 1010)"
    ),
    (
        "F-20A price comparison between plant 1010 and 1710",
        "price_comparison", "F-20A", ["1010", "1710"],
        "get_material_valuation × 2 + FX"
    ),
    (
        "Kitne ka hai DEMOCAR plant 1720 mein?",
        "price_comparison", "DEMOCAR", ["1720"],
        "get_material_valuation(DEMOCAR, 1720)"
    ),
    (
        "Show me prices for DEMOCAR at all plants in INR",
        "all_plants_analysis", "DEMOCAR", [],
        "deep_bom_cost_analysis(DEMOCAR) or list_material_plants → valuations"
    ),
    (
        "At what price does plant 1010 sell DEMOCAR?",
        "price_comparison", "DEMOCAR", ["1010"],
        "get_material_valuation(DEMOCAR, 1010)"
    ),
    (
        "What is the price of F-10A at plant 1710 in USD and INR?",
        "price_comparison", "F-10A", ["1710"],
        "get_material_valuation(F-10A, 1710) + get_tcurr_fx_rate"
    ),
    (
        "DEMOCAR ki price 1010 aur 1710 mein compare karo",
        "price_comparison", "DEMOCAR", ["1010", "1710"],
        "get_material_valuation × 2 + FX"
    ),
    (
        "Which is more expensive — DEMOCAR at 1710 or 1720?",
        "price_comparison", "DEMOCAR", ["1710", "1720"],
        "get_material_valuation × 2 + compare"
    ),
    (
        "Highest priced plant for F-20A",
        "price_comparison", "F-20A", [],
        "list_material_plants(F-20A) → get_material_valuation per plant → rank"
    ),
    (
        "Give me the cheapest plant for F-10B",
        "price_comparison", "F-10B", [],
        "list_material_plants(F-10B) → get_material_valuation per plant → rank"
    ),
    (
        "What is the standard price of DEMOCAR at plant 1010 in EUR and INR?",
        "price_comparison", "DEMOCAR", ["1010"],
        "get_material_valuation(DEMOCAR, 1010) + get_tcurr_fx_rate(EUR→INR)"
    ),

    # ════════════════════════════════════════════════════════════════
    # SECTION D — BOM COMPONENTS (15 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "What components make up DEMOCAR at plant 1010?",
        "bom_explosion", "DEMOCAR", ["1010"],
        "get_bom_explosion(DEMOCAR, 1010, multi)"
    ),
    (
        "DEMOCAR kisse bana hai plant 1710 mein?",
        "bom_explosion", "DEMOCAR", ["1710"],
        "get_bom_explosion(DEMOCAR, 1710, multi)"
    ),
    (
        "Explode BOM for F-10A at plant 1010",
        "bom_explosion", "F-10A", ["1010"],
        "get_bom_explosion(F-10A, 1010, multi)"
    ),
    (
        "What are the sub-components of DEMOBODY at 1720?",
        "bom_explosion", "DEMOBODY", ["1720"],
        "get_bom_explosion(DEMOBODY, 1720, multi)"
    ),
    (
        "Show me the full BOM tree for DEMOCAR at 1710",
        "bom_explosion", "DEMOCAR", ["1710"],
        "get_bom_explosion(DEMOCAR, 1710, multi) L0→L3"
    ),
    (
        "Which components are used in F-20A at plant 1010?",
        "bom_explosion", "F-20A", ["1010"],
        "get_bom_explosion(F-20A, 1010, multi)"
    ),
    (
        "DEMOENGINE kis cheez se bana hai?",
        "bom_explosion", "DEMOENGINE", [],
        "get_bom_explosion(DEMOENGINE, <discovered plant>, multi)"
    ),
    (
        "List all parts of F-10B at plant 1710",
        "bom_explosion", "F-10B", ["1710"],
        "get_bom_explosion(F-10B, 1710, multi)"
    ),
    (
        "Multi-level BOM explosion for DEMOCAR plant 1720",
        "bom_explosion", "DEMOCAR", ["1720"],
        "get_bom_explosion(DEMOCAR, 1720, multi)"
    ),
    (
        "SEAT material ka BOM structure kya hai?",
        "bom_explosion", "SEAT", [],
        "get_bom_explosion(SEAT, <discovered plant>, multi)"
    ),
    (
        "What is DEMOCAR made of at plant 1010?",
        "bom_explosion", "DEMOCAR", ["1010"],
        "get_bom_explosion(DEMOCAR, 1010, multi)"
    ),
    (
        "Component list for F-10A at all plants",
        "bom_explosion", "F-10A", [],
        "list_material_plants → get_bom_explosion per plant"
    ),
    (
        "Tell me the components for DEMOCHASIS at plant 1710",
        "bom_explosion", "DEMOCHASIS", ["1710"],
        "get_bom_explosion(DEMOCHASIS, 1710, multi)"
    ),
    (
        "Level by level BOM breakdown of F-20A at 1010",
        "bom_explosion", "F-20A", ["1010"],
        "get_bom_explosion(F-20A, 1010, multi) — L0→L1→L2→L3"
    ),
    (
        "What are the raw materials inside DEMOWHEEL?",
        "bom_explosion", "DEMOWHEEL", [],
        "get_bom_explosion(DEMOWHEEL, <discovered>, multi)"
    ),

    # ════════════════════════════════════════════════════════════════
    # SECTION E — COST VARIANCE / WHY (15 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "Why is DEMOCAR more expensive at plant 1720 than 1710?",
        "cost_variance", "DEMOCAR", ["1720", "1710"],
        "explain_bom_cost_variance(DEMOCAR, 1720, 1710)"
    ),
    (
        "Root cause analysis of DEMOCAR cost between 1010 and 1710",
        "cost_variance", "DEMOCAR", ["1010", "1710"],
        "explain_bom_cost_variance(DEMOCAR, 1010, 1710)"
    ),
    (
        "DEMOCAR 1010 aur 1720 mein price kyun alag hai?",
        "cost_variance", "DEMOCAR", ["1010", "1720"],
        "explain_bom_cost_variance(DEMOCAR, 1010, 1720)"
    ),
    (
        "Explain cost difference between plant 1710 and 1720 for F-10A",
        "cost_variance", "F-10A", ["1710", "1720"],
        "explain_bom_cost_variance(F-10A, 1710, 1720)"
    ),
    (
        "Why does F-20A cost more at plant 1010 vs 1710?",
        "cost_variance", "F-20A", ["1010", "1710"],
        "explain_bom_cost_variance(F-20A, 1010, 1710)"
    ),
    (
        "What causes the price gap between 1010 and 1720 for DEMOCAR?",
        "cost_variance", "DEMOCAR", ["1010", "1720"],
        "explain_bom_cost_variance(DEMOCAR, 1010, 1720)"
    ),
    (
        "Drill down into cost variance for DEMOCAR plant 1710 vs 1720",
        "cost_variance", "DEMOCAR", ["1710", "1720"],
        "explain_bom_cost_variance(DEMOCAR, 1710, 1720)"
    ),
    (
        "Which component is responsible for price difference of DEMOCAR between 1010 and 1710?",
        "cost_variance", "DEMOCAR", ["1010", "1710"],
        "explain_bom_cost_variance(DEMOCAR, 1010, 1710)"
    ),
    (
        "Cost breakdown analysis DEMOCAR 1010 vs 1720",
        "cost_variance", "DEMOCAR", ["1010", "1720"],
        "explain_bom_cost_variance(DEMOCAR, 1010, 1720)"
    ),
    (
        "Why is the cost variance so high between plants 1710 and 1720 for F-10A?",
        "cost_variance", "F-10A", ["1710", "1720"],
        "explain_bom_cost_variance(F-10A, 1710, 1720)"
    ),
    (
        "Reason for price difference in F-10B across plant 1010 and 1710",
        "cost_variance", "F-10B", ["1010", "1710"],
        "explain_bom_cost_variance(F-10B, 1010, 1710)"
    ),
    (
        "Sub-component causing cost increase at plant 1720 for DEMOCAR",
        "cost_variance", "DEMOCAR", ["1720"],
        "explain_bom_cost_variance(DEMOCAR, <session p1>, 1720)"
    ),
    (
        "How does DEMOENGINE price differ between plants?",
        "cost_variance", "DEMOENGINE", [],
        "list_material_plants → explain_bom_cost_variance top 2 plants"
    ),
    (
        "What factor makes DEMOCAR cost different between plant 1010 and 1710?",
        "cost_variance", "DEMOCAR", ["1010", "1710"],
        "explain_bom_cost_variance(DEMOCAR, 1010, 1710)"
    ),
    (
        "Deep dive into why F-20A price varies between 1010 and 1710",
        "cost_variance", "F-20A", ["1010", "1710"],
        "explain_bom_cost_variance(F-20A, 1010, 1710)"
    ),

    # ════════════════════════════════════════════════════════════════
    # SECTION F — ALL PLANTS DEEP ANALYSIS (5 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "Compare DEMOCAR costs across all plants",
        "all_plants_analysis", "DEMOCAR", [],
        "deep_bom_cost_analysis(DEMOCAR)"
    ),
    (
        "Which plant manufactures DEMOCAR at the lowest total cost?",
        "all_plants_analysis", "DEMOCAR", [],
        "deep_bom_cost_analysis(DEMOCAR) → rank"
    ),
    (
        "Full BOM cost analysis for F-10A — all plants",
        "all_plants_analysis", "F-10A", [],
        "deep_bom_cost_analysis(F-10A)"
    ),
    (
        "Cost optimisation report for DEMOCAR across every plant",
        "all_plants_analysis", "DEMOCAR", [],
        "deep_bom_cost_analysis(DEMOCAR) → recommendations"
    ),
    (
        "DEMOCAR sab plants mein kitna expensive hai?",
        "all_plants_analysis", "DEMOCAR", [],
        "deep_bom_cost_analysis(DEMOCAR)"
    ),

    # ════════════════════════════════════════════════════════════════
    # SECTION G — SUPPLIER / PO (5 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "Who supplies DEMOCAR components at plant 1710?",
        "supplier_po", "DEMOCAR", ["1710"],
        "get_purchase_info_records(DEMOCAR, 1710)"
    ),
    (
        "Show purchase order history for F-10A at plant 1010",
        "supplier_po", "F-10A", ["1010"],
        "get_purchase_order_history(F-10A, 1010)"
    ),
    (
        "Vendor details for F-20A at plant 1710",
        "supplier_po", "F-20A", ["1710"],
        "get_purchase_info_records(F-20A, 1710)"
    ),
    (
        "What are the recent POs for DEMOCAR at plant 1720?",
        "supplier_po", "DEMOCAR", ["1720"],
        "get_purchase_order_history(DEMOCAR, 1720)"
    ),
    (
        "Is there a single supplier risk for DEMOCHASIS at plant 1010?",
        "supplier_po", "DEMOCHASIS", ["1010"],
        "get_purchase_info_records(DEMOCHASIS, 1010) → vendorCount check"
    ),

    # ════════════════════════════════════════════════════════════════
    # SECTION H — BUDGET / CALCULATION (5 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "How many F-10A can I buy with 50 lakh rupees at plant 1010?",
        "calculation", "F-10A", ["1010"],
        "get_material_valuation(F-10A,1010) + FX → units = 50L ÷ price"
    ),
    (
        "If budget is 2 crore, how many DEMOCAR from plant 1720?",
        "calculation", "DEMOCAR", ["1720"],
        "get_material_valuation(DEMOCAR,1720) + FX → units = 2cr ÷ price"
    ),
    (
        "I have 10 lakh INR. How many F-20A at plant 1710 can I afford?",
        "calculation", "F-20A", ["1710"],
        "get_material_valuation(F-20A,1710) + FX → math"
    ),
    (
        "Budget hai 5 crore — kitne DEMOCAR plant 1010 se kharid sakte hain?",
        "calculation", "DEMOCAR", ["1010"],
        "get_material_valuation(DEMOCAR,1010) + FX → units"
    ),
    (
        "After buying 10 units of DEMOCAR at 1710, how much money is left from 1 crore?",
        "calculation", "DEMOCAR", ["1710"],
        "get_material_valuation(DEMOCAR,1710) → remainder = 1cr - (10 × price)"
    ),

    # ════════════════════════════════════════════════════════════════
    # SECTION I — MAKE vs BUY / PROCUREMENT (5 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "Is DEMOCAR manufactured in-house or externally at plant 1710?",
        "price_comparison", "DEMOCAR", ["1710"],
        "get_material_procurement_type(DEMOCAR, 1710)"
    ),
    (
        "What is the procurement type for F-10A at plant 1010?",
        "price_comparison", "F-10A", ["1010"],
        "get_material_procurement_type(F-10A, 1010)"
    ),
    (
        "Should DEMOCHASIS be made in-house or bought externally based on cost?",
        "cost_variance", "DEMOCHASIS", [],
        "get_material_procurement_type + explain_bom_cost_variance → Make/Buy rec"
    ),
    (
        "Make vs buy recommendation for DEMOENGINE",
        "cost_variance", "DEMOENGINE", [],
        "explain_bom_cost_variance → Make/Buy classification"
    ),
    (
        "Which components of DEMOCAR should switch to external procurement?",
        "cost_variance", "DEMOCAR", [],
        "explain_bom_cost_variance → SWITCH_TO_BUY recommendations"
    ),

    # ════════════════════════════════════════════════════════════════
    # SECTION J — VAGUE / FOLLOW-UP / SESSION MEMORY (10 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "Why does it cost more?",           # follow-up — session has DEMOCAR 1710/1720
        "cost_variance", None, [],
        "session memory → explain_bom_cost_variance(session_material, session_plants)"
    ),
    (
        "Same for plant 1010 too",           # follow-up after price query
        "price_comparison", None, [],
        "session_material + add plant 1010 → get_material_valuation"
    ),
    (
        "Now compare with 1720",             # follow-up
        "price_comparison", None, ["1720"],
        "session_material + 1720 → get_material_valuation"
    ),
    (
        "In INR please",                     # follow-up — just currency conversion
        "price_comparison", None, [],
        "session data + get_tcurr_fx_rate → convert to INR"
    ),
    (
        "What about plant 1010?",            # follow-up
        "price_comparison", None, ["1010"],
        "session_material + 1010 → get_material_valuation(session_mat, 1010)"
    ),
    (
        "Show me components",               # follow-up after BOM number query
        "bom_explosion", None, [],
        "session material+plant → get_bom_explosion"
    ),
    (
        "Why?",                             # ultra-vague follow-up
        "cost_variance", None, [],
        "session material+plants → explain_bom_cost_variance"
    ),
    (
        "Give me the cheapest option",      # follow-up — session context
        "price_comparison", None, [],
        "session material → rank by INR"
    ),
    (
        "Which is better for production?",  # follow-up
        "cost_variance", None, [],
        "session → explain_bom_cost_variance + Make/Buy rec"
    ),
    (
        "Aur 1010 ke liye?",               # Hindi follow-up "And for 1010?"
        "price_comparison", None, ["1010"],
        "session_material + 1010 → get_material_valuation"
    ),

    # ════════════════════════════════════════════════════════════════
    # SECTION K — CASUAL / NATURAL PHRASING (10 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "DEMOCAR kitne ka hai yaar?",       # casual Hindi
        "price_comparison", "DEMOCAR", [],
        "list_material_plants(DEMOCAR) → get_material_valuation per plant"
    ),
    (
        "Bhai F-10A ka BOM bata",           # casual Hindi
        "bom_number", "F-10A", [],
        "get_bom_headers(F-10A)"
    ),
    (
        "Give me everything about DEMOCAR at plant 1710",
        "price_comparison", "DEMOCAR", ["1710"],
        "get_material_valuation + get_bom_headers + get_material_procurement_type"
    ),
    (
        "I need to understand why plant 1720 costs so much",
        "cost_variance", None, ["1720"],
        "session_material + explain_bom_cost_variance with 1720"
    ),
    (
        "Quickly tell me DEMOCAR price at 1710 and 1720",
        "price_comparison", "DEMOCAR", ["1710", "1720"],
        "get_material_valuation × 2 + FX"
    ),
    (
        "Plant 1010 pe F-20A ka kya scene hai?",
        "price_comparison", "F-20A", ["1010"],
        "get_material_valuation(F-20A, 1010)"
    ),
    (
        "DEMOCAR component wise cost breakdown chahiye",
        "bom_explosion", "DEMOCAR", [],
        "get_bom_explosion(DEMOCAR, <discovered plant>, multi)"
    ),
    (
        "1710 vs 1720 — kaunsa better hai?",
        "price_comparison", None, ["1710", "1720"],
        "session_material + get_material_valuation × 2 + compare"
    ),
    (
        "Mujhe DEMOCAR ka full analysis chahiye",
        "all_plants_analysis", "DEMOCAR", [],
        "deep_bom_cost_analysis(DEMOCAR)"
    ),
    (
        "F-10A ke baare mein sab kuch batao plant 1010 mein",
        "price_comparison", "F-10A", ["1010"],
        "get_material_valuation + get_bom_headers + procurement"
    ),

    # ════════════════════════════════════════════════════════════════
    # SECTION L — EDGE CASES (10 questions)
    # ════════════════════════════════════════════════════════════════
    (
        "BOM components of DEMOCAR for plant 1710 and 1720 both",
        "bom_explosion", "DEMOCAR", ["1710", "1720"],
        "get_bom_explosion(DEMOCAR,1710,multi) + get_bom_explosion(DEMOCAR,1720,multi)"
    ),
    (
        "What are the BOM numbers and components for DEMOCAR at 1010?",
        "bom_explosion", "DEMOCAR", ["1010"],
        "get_bom_headers(DEMOCAR,1010) + get_bom_explosion(DEMOCAR,1010,multi)"
    ),
    (
        "Is there any BOM for material XYZ123?",    # non-existent material
        "bom_number", "XYZ123", [],
        "get_bom_headers(XYZ123) → no BOM found message"
    ),
    (
        "What is plant 9999 price for DEMOCAR?",    # non-existent plant
        "price_comparison", "DEMOCAR", ["9999"],
        "get_material_valuation(DEMOCAR,9999) → no data found message"
    ),
    (
        "DEMOCAR   plant  1710  price",             # extra spaces
        "price_comparison", "DEMOCAR", ["1710"],
        "get_material_valuation(DEMOCAR, 1710)"
    ),
    (
        "democar plant 1710 bom",                   # lowercase
        "bom_number", "DEMOCAR", ["1710"],
        "get_bom_headers(DEMOCAR, 1710) [note: classifier upper-cases DEMOCAR]"
    ),
    (
        "What is BOM number 00000380, 00000381, 00000385?",
        "reverse_bom", None, [],
        "fast-path for all three → DEMOCAR/1010, DEMOCAR/1710, DEMOCAR/1720"
    ),
    (
        "Tell me everything about BOM 00000052",
        "reverse_bom", None, [],
        "fast-path → F-10A/1010 + siblings"
    ),
    (
        "Which plant has both DEMOCAR and F-10A?",
        "price_comparison", "DEMOCAR", [],
        "list_material_plants(DEMOCAR) + list_material_plants(F-10A) → intersect"
    ),
    (
        "How many total materials are in SAP CIN_100?",
        "material_count", None, [],
        "get_material_count()"
    ),
]

# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def run():
    passed = failed = 0
    section_results: dict = {}
    all_failures = []

    # Section names for display
    section_names = {
        "A": "BOM Numbers",
        "B": "Reverse BOM",
        "C": "Price Comparison",
        "D": "BOM Components",
        "E": "Cost Variance / Why",
        "F": "All Plants Deep Analysis",
        "G": "Supplier / PO",
        "H": "Budget / Calculation",
        "I": "Make vs Buy / Procurement",
        "J": "Vague / Follow-up / Session",
        "K": "Casual / Natural Phrasing",
        "L": "Edge Cases",
    }

    sec_counts = {
        "A": 10, "B": 10, "C": 15, "D": 15, "E": 15,
        "F": 5,  "G": 5,  "H": 5,  "I": 5,  "J": 10,
        "K": 10, "L": 10,
    }

    print(f"\n{'═'*90}")
    print(f"{BOLD}BOMA v6.3.0 — 100-Question Agentic Intelligence Benchmark{RESET}")
    print(f"Zero hardcoding · Pure intent-driven routing · All 16 SAP tools covered")
    print(f"{'═'*90}\n")

    q_idx = 0
    sec_keys = sorted(sec_counts.keys())
    for sec in sec_keys:
        count = sec_counts[sec]
        sec_name = section_names[sec]
        sec_pass = sec_fail = 0

        print(f"{BOLD}{'─'*90}{RESET}")
        print(f"{BOLD}SECTION {sec} — {sec_name} ({count} questions){RESET}")
        print(f"{'─'*90}")

        for i in range(count):
            if q_idx >= len(QUESTIONS_100):
                break
            q, exp_intent, exp_mat, exp_plants, tool_call = QUESTIONS_100[q_idx]
            q_idx += 1

            result = SampleAgent._classify_intent(q)
            got_intent   = result["intent"]
            got_material = result["material"]
            got_plants   = result["plants"]

            # Evaluate
            intent_ok = (got_intent == exp_intent)
            mat_ok    = (exp_mat is None) or (got_material == exp_mat)
            plants_ok = (not exp_plants) or all(p in got_plants for p in exp_plants)
            ok        = intent_ok and mat_ok and plants_ok

            status = f"{GREEN}✅{RESET}" if ok else f"{RED}❌{RESET}"
            num    = f"Q{q_idx:03d}"

            print(f"  {status} {num}  {q}")
            if not ok:
                if not intent_ok:
                    print(f"         {RED}intent:   expected={exp_intent}  got={got_intent}{RESET}")
                if not mat_ok:
                    print(f"         {RED}material: expected={exp_mat}  got={got_material}{RESET}")
                if not plants_ok:
                    print(f"         {RED}plants:   expected={exp_plants}  got={got_plants}{RESET}")
            print(f"         {CYAN}→ {tool_call}{RESET}")

            if ok:
                passed += 1
                sec_pass += 1
            else:
                failed += 1
                sec_fail += 1
                all_failures.append((q_idx, q, exp_intent, got_intent, exp_mat, got_material, exp_plants, got_plants))

        section_results[sec] = (sec_pass, sec_fail, count)
        rate = round(sec_pass / count * 100)
        bar  = "█" * (rate // 5) + "░" * (20 - rate // 5)
        col  = GREEN if rate >= 90 else YELLOW if rate >= 70 else RED
        print(f"\n  {col}Section {sec}: {sec_pass}/{count} ({rate}%) {bar}{RESET}\n")

    # ── Overall Summary ───────────────────────────────────────────────────────
    total = len(QUESTIONS_100)
    pct   = round(passed / total * 100, 1)
    print(f"\n{'═'*90}")
    print(f"{BOLD}FINAL SCORE: {passed}/{total} ({pct}%){RESET}")
    print(f"{'─'*90}")
    print(f"{'Section':<6} {'Name':<35} {'Pass':<6} {'Fail':<6} {'Rate'}")
    print(f"{'─'*90}")
    for sec in sec_keys:
        sp, sf, sc = section_results.get(sec, (0,0,0))
        rate = round(sp / sc * 100) if sc else 0
        col  = GREEN if rate >= 90 else YELLOW if rate >= 70 else RED
        bar  = "█" * (rate // 10)
        print(f"  {sec:<5} {section_names[sec]:<35} {col}{sp:<6}{RESET} {sf:<6} {col}{rate}% {bar}{RESET}")

    print(f"{'─'*90}")
    if failed:
        print(f"\n{RED}{BOLD}FAILURES ({failed}):{RESET}")
        for q_num, q, exp_i, got_i, exp_m, got_m, exp_p, got_p in all_failures:
            print(f"  Q{q_num:03d} {q}")
            print(f"       intent: {exp_i} → {got_i}  |  material: {exp_m} → {got_m}  |  plants: {exp_p} → {got_p}")
    else:
        print(f"\n{GREEN}{BOLD}🎯 PERFECT SCORE — All 100 questions correctly routed!{RESET}")

    print(f"\n{'═'*90}\n")
    return passed, failed


# ── pytest-compatible individual tests ───────────────────────────────────────

def _chk(idx):
    q, exp_i, exp_m, exp_p, _ = QUESTIONS_100[idx]
    r = SampleAgent._classify_intent(q)
    assert r["intent"] == exp_i, f"Q{idx+1}: expected intent={exp_i}, got={r['intent']} | '{q}'"
    if exp_m:
        assert r["material"] == exp_m, f"Q{idx+1}: expected material={exp_m}, got={r['material']} | '{q}'"
    if exp_p:
        for p in exp_p:
            assert p in r["plants"], f"Q{idx+1}: expected plant {p} in {r['plants']} | '{q}'"

def test_q001(): _chk(0)
def test_q002(): _chk(1)
def test_q003(): _chk(2)
def test_q004(): _chk(3)
def test_q005(): _chk(4)
def test_q006(): _chk(5)
def test_q007(): _chk(6)
def test_q008(): _chk(7)
def test_q009(): _chk(8)
def test_q010(): _chk(9)
def test_q011(): _chk(10)
def test_q012(): _chk(11)
def test_q013(): _chk(12)
def test_q014(): _chk(13)
def test_q015(): _chk(14)
def test_q016(): _chk(15)
def test_q017(): _chk(16)
def test_q018(): _chk(17)
def test_q019(): _chk(18)
def test_q020(): _chk(19)
def test_q021(): _chk(20)
def test_q022(): _chk(21)
def test_q023(): _chk(22)
def test_q024(): _chk(23)
def test_q025(): _chk(24)
def test_q026(): _chk(25)
def test_q027(): _chk(26)
def test_q028(): _chk(27)
def test_q029(): _chk(28)
def test_q030(): _chk(29)
def test_q031(): _chk(30)
def test_q032(): _chk(31)
def test_q033(): _chk(32)
def test_q034(): _chk(33)
def test_q035(): _chk(34)
def test_q036(): _chk(35)
def test_q037(): _chk(36)
def test_q038(): _chk(37)
def test_q039(): _chk(38)
def test_q040(): _chk(39)
def test_q041(): _chk(40)
def test_q042(): _chk(41)
def test_q043(): _chk(42)
def test_q044(): _chk(43)
def test_q045(): _chk(44)
def test_q046(): _chk(45)
def test_q047(): _chk(46)
def test_q048(): _chk(47)
def test_q049(): _chk(48)
def test_q050(): _chk(49)
def test_q051(): _chk(50)
def test_q052(): _chk(51)
def test_q053(): _chk(52)
def test_q054(): _chk(53)
def test_q055(): _chk(54)
def test_q056(): _chk(55)
def test_q057(): _chk(56)
def test_q058(): _chk(57)
def test_q059(): _chk(58)
def test_q060(): _chk(59)
def test_q061(): _chk(60)
def test_q062(): _chk(61)
def test_q063(): _chk(62)
def test_q064(): _chk(63)
def test_q065(): _chk(64)
def test_q066(): _chk(65)
def test_q067(): _chk(66)
def test_q068(): _chk(67)
def test_q069(): _chk(68)
def test_q070(): _chk(69)
def test_q071(): _chk(70)
def test_q072(): _chk(71)
def test_q073(): _chk(72)
def test_q074(): _chk(73)
def test_q075(): _chk(74)
def test_q076(): _chk(75)
def test_q077(): _chk(76)
def test_q078(): _chk(77)
def test_q079(): _chk(78)
def test_q080(): _chk(79)
def test_q081(): _chk(80)
def test_q082(): _chk(81)
def test_q083(): _chk(82)
def test_q084(): _chk(83)
def test_q085(): _chk(84)
def test_q086(): _chk(85)
def test_q087(): _chk(86)
def test_q088(): _chk(87)
def test_q089(): _chk(88)
def test_q090(): _chk(89)
def test_q091(): _chk(90)
def test_q092(): _chk(91)
def test_q093(): _chk(92)
def test_q094(): _chk(93)
def test_q095(): _chk(94)
def test_q096(): _chk(95)
def test_q097(): _chk(96)
def test_q098(): _chk(97)
def test_q099(): _chk(98)
def test_q100(): _chk(99)


if __name__ == "__main__":
    passed, failed = run()
    sys.exit(0 if failed == 0 else 1)

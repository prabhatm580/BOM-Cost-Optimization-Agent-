"""
BOM Cost Optimization Tools

7 tools for cross-plant BOM cost analysis, Make/Buy/Strategic Buy classification,
and workflow triggering. S/4HANA data is retrieved directly via the BTP Destination
Service (Cloud Foundry) or via MCP servers (Joule Studio, when JOULE_RUNTIME=1).

Milestone logging pattern:
  [M1.achieved: Make/Buy classified for {material}, variance={pct}%]
  [M1.missed: classification failed for {material}, reason={reason}]
  [M2.achieved: workflow triggered for {analysis_id}]
  [M3.achieved: cross-plant comparison completed for {vehicle}]
"""

import json
import logging
import os
from typing import Any, Optional

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from s4hana_client import Client as S4Client

logger = logging.getLogger(__name__)

# CAP service base URL
CAP_SERVICE_URL = os.environ.get("CAP_SERVICE_URL", "http://localhost:4004")

# ── Persistent feedback store ─────────────────────────────────────────────────
# Persists to /tmp/boma_feedback.json (survives short CF restarts/crashes)
# Structure: list of {question, answer, vote: "up"|"down", ts, question_type}
_FEEDBACK_STORE: list[dict] = []
_FEEDBACK_FILE = "/tmp/boma_feedback.json"

def _detect_question_type(question: str) -> str:
    """Classify question type for smarter training grouping."""
    q = question.lower()
    if any(k in q for k in ["bom number", "bill of material", "stlnr"]):
        return "bom_number_lookup"
    if any(k in q for k in ["material against bom", "which material", "material for bom", "elaborate bom"]):
        return "reverse_bom_lookup"
    if any(k in q for k in ["explode", "components", "bom structure", "cs12"]):
        return "bom_explosion"
    if any(k in q for k in ["cost", "price", "valuation", "compare plant", "cheaper"]):
        return "cost_comparison"
    if any(k in q for k in ["how many material", "count material", "total material"]):
        return "material_count"
    if any(k in q for k in ["make", "buy", "strategic", "classify"]):
        return "make_buy_classification"
    if any(k in q for k in ["fx", "exchange rate", "currency", "inr", "usd", "eur"]):
        return "fx_conversion"
    if any(k in q for k in ["plant", "which plant", "best plant"]):
        return "plant_discovery"
    return "general"

def _load_feedback_from_disk() -> None:
    """Load persisted feedback from disk on startup."""
    global _FEEDBACK_STORE
    try:
        if os.path.isfile(_FEEDBACK_FILE):
            with open(_FEEDBACK_FILE, "r") as f:
                data = json.load(f)
            _FEEDBACK_STORE = data if isinstance(data, list) else []
            logger.info("Loaded %d feedback entries from disk", len(_FEEDBACK_STORE))
    except Exception as e:
        logger.warning("Could not load feedback from disk: %s", e)
        _FEEDBACK_STORE = []

def _save_feedback_to_disk() -> None:
    """Persist feedback store to disk."""
    try:
        with open(_FEEDBACK_FILE, "w") as f:
            json.dump(_FEEDBACK_STORE[-500:], f)  # keep last 500
    except Exception as e:
        logger.warning("Could not save feedback to disk: %s", e)

# Load on module import
_load_feedback_from_disk()

def record_feedback(question: str, answer: str, vote: str) -> None:
    """
    Called by /api/feedback endpoint to record user vote.
    Persists to disk AND memory so agent learns across restarts.
    """
    import time as _time
    entry = {
        "question": question[:400],
        "answer": answer[:800],
        "vote": vote,           # "up" or "down"
        "ts": _time.time(),
        "question_type": _detect_question_type(question),
    }
    _FEEDBACK_STORE.append(entry)
    # Keep last 500 votes in memory
    if len(_FEEDBACK_STORE) > 500:
        _FEEDBACK_STORE.pop(0)
    _save_feedback_to_disk()
    logger.info("Feedback recorded: %s | vote=%s | type=%s",
                question[:80], vote, entry["question_type"])

def get_feedback_summary() -> str:
    """
    Return compact feedback summary for injection into agent context.
    Groups by question_type — highlights patterns the agent should reinforce or avoid.
    """
    if not _FEEDBACK_STORE:
        return ""
    from collections import defaultdict

    # Group by question_type + first 100 chars of question
    type_groups: dict = defaultdict(lambda: {"up": 0, "down": 0, "examples": []})
    q_groups: dict    = defaultdict(lambda: {"up": 0, "down": 0, "answer": "", "question": ""})

    for fb in _FEEDBACK_STORE[-200:]:
        qt = fb.get("question_type", "general")
        type_groups[qt][fb["vote"]] += 1

        key = fb["question"][:100]
        q_groups[key][fb["vote"]] += 1
        q_groups[key]["answer"] = fb["answer"][:300]
        q_groups[key]["question"] = fb["question"][:100]
        if fb["vote"] == "up":
            type_groups[qt]["examples"].append({
                "q": fb["question"][:100],
                "a": fb["answer"][:200],
            })

    lines = []

    # Per-type patterns
    for qt, counts in type_groups.items():
        total = counts["up"] + counts["down"]
        if total >= 2:
            pct = int(counts["up"] / total * 100)
            lines.append(f"📊 {qt}: {pct}% approval ({counts['up']}👍 {counts['down']}👎, n={total})")

    # Specific good answers (≥2 thumbs up)
    good_examples = []
    for key, v in q_groups.items():
        if v["up"] >= 2:
            good_examples.append(
                f'Q: "{v["question"]}"\nA: {v["answer"][:200]}'
            )
        elif v["down"] >= 2:
            lines.append(f'❌ AVOID THIS PATTERN for: "{key}" — user marked bad {v["down"]} times')

    if good_examples:
        lines.append("\n## FEW-SHOT EXAMPLES (from user-approved answers):")
        lines.extend(good_examples[:5])  # top 5 approved Q&A pairs

    return "\n".join(lines) if lines else ""

def get_training_examples(question_type: str | None = None) -> list[dict]:
    """
    Return top-voted Q&A pairs for a given question type.
    Used to inject few-shot examples into the agent's context.
    """
    from collections import defaultdict
    groups: dict = defaultdict(lambda: {"up": 0, "down": 0, "question": "", "answer": "", "question_type": ""})
    for fb in _FEEDBACK_STORE:
        qt = fb.get("question_type", "general")
        if question_type and qt != question_type:
            continue
        key = fb["question"][:100]
        groups[key][fb["vote"]] += 1
        groups[key]["question"] = fb["question"]
        groups[key]["answer"] = fb["answer"]
        groups[key]["question_type"] = qt
    # Return only positively-voted examples, sorted by votes
    positive = [(k, v) for k, v in groups.items() if v["up"] > v["down"] and v["up"] >= 1]
    positive.sort(key=lambda x: x[1]["up"], reverse=True)
    return [{"question": v["question"], "answer": v["answer"],
             "question_type": v["question_type"], "votes": v["up"]}
            for _, v in positive[:10]]

def get_feedback_stats() -> dict:
    """Return stats about the feedback store for /api/feedback/stats endpoint."""
    from collections import defaultdict, Counter
    total = len(_FEEDBACK_STORE)
    up = sum(1 for f in _FEEDBACK_STORE if f.get("vote") == "up")
    down = total - up
    type_counts = Counter(f.get("question_type", "general") for f in _FEEDBACK_STORE)
    return {
        "total": total,
        "thumbsUp": up,
        "thumbsDown": down,
        "approvalRate": round(up / total * 100, 1) if total else 0,
        "byQuestionType": dict(type_counts),
        "recentCount": sum(1 for f in _FEEDBACK_STORE[-20:] if f.get("vote") == "up"),
    }

# S/4HANA destination — CIN_100 (SAP ECC/S4 client 100, on-premise via BTP Connectivity)
# Override via env var S4HANA_DESTINATION_NAME if needed

# ---------------------------------------------------------------------------
# S/4HANA service root constants (from Phase 2.5 metadata)
# ---------------------------------------------------------------------------
BOM_ROOT      = "/sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV_0002"
BOM_ROOT_ALT  = "/sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV"  # on-premise alias without _0002
PRODUCT_ROOT  = "/sap/opu/odata/sap/API_PRODUCT_SRV"
PIR_ROOT      = "/sap/opu/odata/sap/API_INFORECORD_PROCESS_SRV"
PO_ROOT       = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"
VALUATION_ROOT = "/sap/opu/odata/sap/API_MATERIAL_VALUATION_SRV"

MAX_PAGE_SIZE = 100


def _enforce_page_size(params: dict, key: str = "$top") -> dict:
    """Cap $top at MAX_PAGE_SIZE."""
    if key in params:
        try:
            params[key] = min(int(params[key]), MAX_PAGE_SIZE)
        except (ValueError, TypeError):
            params[key] = MAX_PAGE_SIZE
    return params


def _ensure_format(params: dict) -> dict:
    """Inject $format=json for OData v2 if not already set."""
    if "$format" not in params:
        params["$format"] = "json"
    return params


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------

class GetBomHeadersInput(BaseModel):
    material_number: str = Field(description="SAP material number to fetch BOM headers/numbers for")
    plant: Optional[str] = Field(default=None, description="Optional plant filter — if omitted, returns BOMs across ALL plants")
    bom_usage: Optional[str] = Field(default=None, description="BOM usage: 1=Production, 2=Engineering, 3=Universal, 4=Plant Maintenance, 5=Costing, 6=Sales")
    bom_category: Optional[str] = Field(default="M", description="BOM category: M=Material BOM (default)")


class GetMaterialByBomNumberInput(BaseModel):
    bom_number: str = Field(description="SAP BOM number (e.g. '00000052') to find the associated material for. This is the REVERSE lookup: BOM number → Material.")
    plant: Optional[str] = Field(default=None, description="Optional plant filter to narrow results")


class GetBomExplosionInput(BaseModel):
    material_number: str = Field(description="SAP material number to explode BOM for")
    plant: str = Field(description="Plant code for BOM explosion")
    bom_usage: Optional[str] = Field(default="1", description="BOM usage: 1=Production (default)")
    explosion_type: Optional[str] = Field(default="multi", description="single=single-level, multi=multi-level")


class GetBomCrossPlantInput(BaseModel):
    vehicle_material: str = Field(description="Vehicle or assembly material number (e.g. 'NEXA-001', 'F-10A')")
    plant1: str = Field(description="First plant code — use numeric SAP plant codes only (e.g. '1010', '1710', '1720')")
    plant2: str = Field(description="Second plant code — use numeric SAP plant codes only (e.g. '1010', '1710', '1720')")
    usage: Optional[str] = Field(default=None, description="BOM usage (e.g. '1' for production)")
    alternative: Optional[str] = Field(default=None, description="BOM alternative (e.g. '1')")


class ListMaterialPlantsInput(BaseModel):
    material_number: str = Field(description="SAP material number to find all plants where it is maintained")

class GetMaterialCountInput(BaseModel):
    material_type: Optional[str] = Field(default=None, description="Optional material type filter (e.g. 'ROH', 'HALB', 'FERT')")

class GetTcurrFxRateInput(BaseModel):
    from_currency: str = Field(description="Source currency code (e.g. 'EUR', 'USD')")
    to_currency: str = Field(description="Target currency code (e.g. 'INR', 'USD')")
    rate_type: Optional[str] = Field(default="M", description="Exchange rate type (M=Standard, B=Bank buying, G=Bank selling)")
    date: Optional[str] = Field(default=None, description="Date for rate lookup YYYYMMDD (defaults to today)")

class GetMaterialProcurementTypeInput(BaseModel):
    material_number: str = Field(description="SAP material number")
    plant: str = Field(description="Plant code — use numeric SAP plant codes (e.g. '1010', '1710', '1720')")


class GetPurchaseInfoRecordsInput(BaseModel):
    material_number: str = Field(description="SAP material number")
    plant: Optional[str] = Field(default=None, description="Plant code (optional — info records are not plant-specific)")
    purchasing_org: Optional[str] = Field(default=None, description="Purchasing organization")


class GetPurchaseOrderHistoryInput(BaseModel):
    material_number: str = Field(description="SAP material number")
    plant: str = Field(description="Plant code")
    date_from: Optional[str] = Field(default=None, description="Start date ISO format (YYYY-MM-DD)")
    date_to: Optional[str] = Field(default=None, description="End date ISO format (YYYY-MM-DD)")


class GetMaterialValuationInput(BaseModel):
    material_number: str = Field(description="SAP material number")
    plant: str = Field(description="Plant code")
    valuation_area: Optional[str] = Field(default=None, description="Valuation area (defaults to plant)")


class GetCheapestMaterialValuationInput(BaseModel):
    material_number: str = Field(
        description=(
            "SAP material number to find cheapest plant for. "
            "This tool automatically discovers all plants, fetches prices at each, "
            "converts to INR, and returns the ranked list with cheapest plant first. "
            "Use this for HYPOTHETICAL PLANNING or any time you need the cheapest plant "
            "WITHOUT specifying a plant upfront."
        )
    )


class RunCostCalculatorInput(BaseModel):
    """Input for deterministic assembly cost + budget calculation."""
    components: list = Field(
        description=(
            "List of component dicts. Each must have: "
            "name (str), qty (float), price (float), currency (str), plant (str). "
            "Example: [{\"name\": \"DEMOCAR\", \"qty\": 1, \"price\": 56772.04, \"currency\": \"EUR\", \"plant\": \"1010\"}]"
        )
    )
    extra_charges: list = Field(
        default_factory=list,
        description=(
            "Additional charges (accessories, labor, taxes). "
            "Each must have: name (str), amount (float), currency (str). "
            "Example: [{\"name\": \"Accessories\", \"amount\": 5000, \"currency\": \"USD\"}]"
        )
    )
    budget: float = Field(
        default=0.0,
        description="Total available budget (0 = skip budget calculation).",
    )
    budget_currency: str = Field(
        default="USD",
        description="Currency of the budget (USD, EUR, INR, etc.).",
    )


class GetBomItemsRawInput(BaseModel):
    bom_number: str = Field(description="SAP BOM number e.g. '00000381'")
    plant: str = Field(description="Plant code e.g. '1710'")
    bom_category: Optional[str] = Field(default="M", description="BOM category M=Material")
    bom_variant: Optional[str] = Field(default="1", description="BOM variant e.g. '1' or '01'")


class ExplainBomCostVarianceInput(BaseModel):
    material_number: str = Field(description="Top-level material to explain cost variance for (e.g. 'DEMOCAR')")
    plant1: str = Field(description="First plant code (e.g. '1010')")
    plant2: str = Field(description="Second plant code (e.g. '1710')")
    max_depth: Optional[int] = Field(default=6, description="Maximum BOM depth to drill into (default 6)")
    variance_threshold_pct: Optional[float] = Field(default=1.0, description="Minimum % variance to report (default 1%)")


class DeepBomCostAnalysisInput(BaseModel):
    material_number: str = Field(description="Top-level material to analyse (e.g. 'DEMOCAR')")
    plants: Optional[list] = Field(default=None, description="List of plant codes. If omitted all plants are discovered automatically.")
    max_depth: Optional[int] = Field(default=3, description="Maximum BOM explosion depth (default 3)")


class TriggerProcurementReviewInput(BaseModel):
    analysis_id: str = Field(description="BomAnalysis UUID from the CAP service")
    priority: Optional[str] = Field(default="Medium", description="Review priority: Low, Medium, High")
    comments: Optional[str] = Field(default=None, description="Optional comments")


class TriggerCostingReviewInput(BaseModel):
    analysis_id: str = Field(description="BomAnalysis UUID from the CAP service")
    cost_center: Optional[str] = Field(default=None, description="Cost center for costing review")
    comments: Optional[str] = Field(default=None, description="Optional comments")


# ---------------------------------------------------------------------------
# Classification logic
# ---------------------------------------------------------------------------

def classify_make_buy(
    material_number: str,
    cost_variance_percent: float,
    procurement_type: str = "",
    has_multiple_suppliers: bool = True,
    strategic_importance: bool = False,
    annual_volume: float = 0,
) -> dict[str, Any]:
    """
    Classify a component as Make / Buy / Strategic Buy.

    Rules:
    - Make: variance < 5% OR procurement_type == 'E' (in-house production)
    - Strategic Buy: variance >= 20% AND (no multiple suppliers OR strategic importance)
    - Buy: variance >= 5% AND not Strategic Buy
    """
    variance = abs(cost_variance_percent)

    if procurement_type == "E" or variance < 5.0:
        recommendation = "Make"
        confidence = "Low" if variance < 2.0 else "Medium"
        reasoning = f"In-house procurement type or low cost variance ({variance:.1f}%)"
    elif variance >= 20.0 and (not has_multiple_suppliers or strategic_importance):
        recommendation = "Strategic Buy"
        confidence = "High"
        reason_parts = []
        if not has_multiple_suppliers:
            reason_parts.append("single supplier dependency")
        if strategic_importance:
            reason_parts.append("strategic component")
        reasoning = f"High variance ({variance:.1f}%) with {', '.join(reason_parts)}"
    else:
        recommendation = "Buy"
        confidence = "High" if variance > 15.0 else ("Medium" if variance >= 5.0 else "Low")
        reasoning = f"Cost variance {variance:.1f}% suggests external sourcing opportunity"

    logger.info(f"[M1.achieved: Make/Buy classified for {material_number}, variance={variance:.1f}%]")

    return {
        "materialNumber": material_number,
        "recommendation": recommendation,
        "confidence": confidence,
        "reasoning": reasoning,
        "costVariancePercent": round(variance, 2),
    }


# ---------------------------------------------------------------------------
# CAP service helper
# ---------------------------------------------------------------------------

async def _call_cap(path: str, method: str = "GET", body: dict | None = None) -> dict:
    """Call the CAP WorkflowTriggerService."""
    url = f"{CAP_SERVICE_URL}{path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        if method == "POST":
            resp = await client.post(url, json=body or {}, headers={"Content-Type": "application/json"})
        else:
            resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def list_material_plants(
    material_number: str,
    s4: S4Client | None = None,
) -> str:
    """
    Discover ALL plants where a material is maintained in S/4HANA.
    Call this FIRST when user asks about a material without specifying plants.
    Returns list of plant codes with procurement type and valuation data.
    This enables autonomous plant discovery without asking the user.
    """
    try:
        if os.environ.get("IBD_TESTING") == "1":
            # ── Confirmed plant list per material from SAP CIN_100 ──────────────
            # Based on BOM/MAST table data confirmed by user. Procurement type
            # must be fetched live — not hardcoded here.
            CONFIRMED_PLANTS = {
                "DEMOCAR": ["1010", "1710", "1720"],
                "F-10A":   ["1010", "1710"],
                "F-10B":   ["1010", "1710"],
                "F-20A":   ["1010", "1710"],
            }
            plant_list = CONFIRMED_PLANTS.get(material_number)
            if plant_list:
                return json.dumps({
                    "materialNumber": material_number,
                    "plantCount": len(plant_list),
                    "plants": [{"plant": p, "procurementType": "", "mrpType": ""} for p in plant_list],
                    "status": "success",
                    "note": "Plant list confirmed from SAP CIN_100. Procurement type fetched live.",
                })
            return json.dumps({
                "materialNumber": material_number,
                "plantCount": 0,
                "plants": [],
                "status": "no_data",
                "note": f"No confirmed plant data for {material_number} in mock. Fetch live from SAP CIN_100.",
            })

        client = s4 or S4Client()
        params = {
            "$filter": f"Product eq '{material_number}'",
            "$select": "Product,Plant,ProcurementType,MRPType",
            "$top": MAX_PAGE_SIZE,
            "$format": "json",
        }
        result = await client.get(f"{PRODUCT_ROOT}/A_ProductPlant", params=params)
        if result.get("error"):
            return json.dumps({"error": result.get("message"), "materialNumber": material_number})

        items = result.get("results", [])
        plants = [
            {
                "plant": i.get("Plant", ""),
                "procurementType": i.get("ProcurementType", ""),
                "mrpType": i.get("MRPType", ""),
            }
            for i in items if i.get("Plant")
        ]
        return json.dumps({
            "materialNumber": material_number,
            "plantCount": len(plants),
            "plants": plants,
            "status": "success" if plants else "no_data",
            "note": "Use these plant codes for valuation and cost comparison" if plants else f"Material {material_number} not found in any plant",
        })
    except Exception as e:
        return json.dumps({"error": str(e), "materialNumber": material_number})


async def get_material_count(
    material_type: str | None = None,
    s4: S4Client | None = None,
) -> str:
    """
    Get total count of materials in S/4HANA CIN_100.
    Queries API_PRODUCT_SRV to count all materials, optionally filtered by type.
    """
    try:
        if os.environ.get("IBD_TESTING") == "1":
            # Material count must be fetched live from SAP — no hardcoded number
            return json.dumps({
                "totalMaterials": "unknown — fetch live from SAP",
                "materialType": material_type or "ALL",
                "system": "CIN_100",
                "status": "no_mock_data",
                "note": "Total material count must be fetched live from A_Product in SAP CIN_100.",
            })

        client = s4 or S4Client()
        params: dict = {"$format": "json", "$top": 1, "$inlinecount": "allpages"}
        if material_type:
            params["$filter"] = f"MaterialType eq '{material_type}'"
        result = await client.get(f"{PRODUCT_ROOT}/A_Product", params=params)
        if result.get("error"):
            return json.dumps({"error": result.get("message")})

        count = result.get("__count") or result.get("@odata.count") or len(result.get("results", []))
        return json.dumps({
            "totalMaterials": int(count) if count else "unknown",
            "materialType": material_type or "ALL",
            "system": "CIN_100",
            "status": "success",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


async def get_tcurr_fx_rate(
    from_currency: str,
    to_currency: str,
    rate_type: str = "M",
    date: str | None = None,
    s4: S4Client | None = None,
) -> str:
    """
    Fetch live FX rate from SAP TCURR table via CIN_100.
    This is the PREFERRED FX source — SAP system rates are authoritative.
    If direct rate not found, automatically tries indirect (from_currency→USD→to_currency).
    Falls back to open.er-api.com if SAP rate unavailable.
    """
    import datetime
    try:
        if os.environ.get("IBD_TESTING") == "1":
            mock_rates = {
                ("USD", "INR"): 96.40, ("EUR", "INR"): 110.37,
                ("GBP", "INR"): 130.0, ("EUR", "USD"): 1.145,
            }
            rate = mock_rates.get((from_currency.upper(), to_currency.upper()))
            if not rate:
                # Try indirect
                usd_to = mock_rates.get(("USD", to_currency.upper()))
                from_to_usd = mock_rates.get((from_currency.upper(), "USD")) or (1 / mock_rates.get(("USD", from_currency.upper()), 1))
                rate = from_to_usd * usd_to if usd_to else None
            return json.dumps({
                "fromCurrency": from_currency, "toCurrency": to_currency,
                "rate": rate or 1.0, "rateType": rate_type,
                "source": "TCURR_MOCK", "date": date or str(datetime.date.today()),
                "method": "direct" if (from_currency.upper(), to_currency.upper()) in mock_rates else "indirect",
            })

        client = s4 or S4Client()
        today = date or datetime.date.today().strftime("%Y%m%d")

        # Try ExchangeRateType service first (S/4HANA)
        # SAP stores rates as FROM_CURR→TO_CURR in TCURR
        # Try direct rate
        params = {
            "$filter": (
                f"ExchangeRateType eq '{rate_type}' and "
                f"FromCurrency eq '{from_currency.upper()}' and "
                f"ToCurrency eq '{to_currency.upper()}'"
            ),
            "$orderby": "ValidityStartDate desc",
            "$top": 1,
            "$format": "json",
        }
        result = await client.get("/sap/opu/odata/sap/API_EXCH_RATE_SRV/A_ExchangeRate", params=params)
        items = result.get("results", []) if not result.get("error") else []

        rate_val = None
        method = "direct"
        if items:
            r = items[0]
            rate_val = float(r.get("ExchangeRate") or r.get("DirectExchangeRate") or 0)

        # Try reverse (SAP sometimes stores inverted)
        if not rate_val:
            params_rev = {
                "$filter": (
                    f"ExchangeRateType eq '{rate_type}' and "
                    f"FromCurrency eq '{to_currency.upper()}' and "
                    f"ToCurrency eq '{from_currency.upper()}'"
                ),
                "$orderby": "ValidityStartDate desc",
                "$top": 1,
                "$format": "json",
            }
            r2 = await client.get("/sap/opu/odata/sap/API_EXCH_RATE_SRV/A_ExchangeRate", params=params_rev)
            r2_items = r2.get("results", []) if not r2.get("error") else []
            if r2_items:
                inv = float(r2_items[0].get("ExchangeRate") or r2_items[0].get("DirectExchangeRate") or 0)
                if inv:
                    rate_val = round(1.0 / inv, 6)
                    method = "inverse"

        # Indirect: from_curr → USD → to_curr
        if not rate_val and from_currency.upper() not in ("USD",) and to_currency.upper() not in ("USD",):
            logger.info("Trying indirect FX: %s→USD→%s", from_currency, to_currency)
            r_from_usd = await get_tcurr_fx_rate("USD", from_currency, rate_type, date, s4)
            r_to_usd = await get_tcurr_fx_rate("USD", to_currency, rate_type, date, s4)
            d_from = json.loads(r_from_usd)
            d_to = json.loads(r_to_usd)
            if d_from.get("rate") and d_to.get("rate") and float(d_from["rate"]) > 0:
                rate_val = round(float(d_to["rate"]) / float(d_from["rate"]), 6)
                method = f"indirect_via_USD"

        # Final fallback: open.er-api.com
        source = "SAP_TCURR"
        if not rate_val:
            import urllib.request as _ureq
            try:
                url = f"https://open.er-api.com/v6/latest/{from_currency.upper()}"
                req = _ureq.Request(url, headers={"Accept": "application/json"})
                with _ureq.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read())
                rate_val = float(data.get("rates", {}).get(to_currency.upper(), 0))
                source = "open.er-api.com"
                method = "api_fallback"
            except Exception as api_err:
                logger.warning("FX API fallback failed: %s", api_err)

        return json.dumps({
            "fromCurrency": from_currency.upper(),
            "toCurrency": to_currency.upper(),
            "rate": rate_val or 0.0,
            "rateType": rate_type,
            "source": source,
            "date": today,
            "method": method,
        })
    except Exception as e:
        return json.dumps({"error": str(e), "fromCurrency": from_currency, "toCurrency": to_currency})


async def get_bom_headers(
    material_number: str,
    plant: str | None = None,
    bom_usage: str | None = None,
    bom_category: str = "M",
    s4: S4Client | None = None,
) -> str:
    """
    Fetch BOM header numbers (STLNR) for a material from SAP CIN_100.
    Returns BOM numbers, category, variant, plant, and usage.
    This is the CORRECT tool when user asks 'what is the BOM number for material X'.
    Uses API_BILL_OF_MATERIAL_SRV_0002/A_BillOfMaterial.
    """
    try:
        if os.environ.get("IBD_TESTING") == "1":
            # ── Confirmed BOM numbers from SAP CIN_100 (source: CS03 / MAST table) ──
            # ONLY plants that ACTUALLY exist in CIN_100 are listed here.
            # DO NOT add a plant entry unless confirmed in SAP.
            REAL_BOM_MAP = {
                # DEMOCAR — exists at 1010, 1710, 1720 (confirmed 07/2026 by I059550)
                ("DEMOCAR", "1010"): "00000380",
                ("DEMOCAR", "1710"): "00000381",
                ("DEMOCAR", "1720"): "00000385",
                # F-10A — exists at 1010 and 1710 ONLY (confirmed 10/2017 by BPINST)
                ("F-10A",   "1010"): "00000052",
                ("F-10A",   "1710"): "00000023",
                # F-10B — exists at 1010 and 1710 ONLY (confirmed 10/2017 by BPINST)
                ("F-10B",   "1010"): "00000053",
                ("F-10B",   "1710"): "00000024",
                # F-20A — exists at 1010 and 1710 ONLY (confirmed 09/2018-09/2019 by BPINST)
                ("F-20A",   "1010"): "00000105",
                ("F-20A",   "1710"): "00000060",
            }
            # Determine which plants to return
            if plant:
                query_plants = [plant]
            else:
                # Return ONLY plants confirmed in SAP for this material
                query_plants = [p for (m, p) in REAL_BOM_MAP if m == material_number]

            plants_data = []
            for p in query_plants:
                bom_num = REAL_BOM_MAP.get((material_number, p))
                if not bom_num:
                    # Plant not confirmed in SAP — skip, do not invent
                    continue
                plants_data.append({
                    "billOfMaterial": bom_num,
                    "billOfMaterialCategory": bom_category or "M",
                    "billOfMaterialVariant": "01",
                    "billOfMaterialVersion": "",
                    "plant": p,
                    "material": material_number,
                    "bomUsage": bom_usage or "1",
                    "bomUsageName": {"1": "Production", "2": "Engineering", "3": "Universal", "5": "Costing"}.get(bom_usage or "1", "Production"),
                })
            return json.dumps({
                "materialNumber": material_number,
                "plant": plant or "ALL",
                "bomCount": len(plants_data),
                "bomHeaders": plants_data,
                "status": "success" if plants_data else "no_bom_found",
                "note": f"Found {len(plants_data)} BOM(s) for {material_number}" if plants_data
                        else f"No BOM found for {material_number} at plant {plant}. Material may not exist at this plant in CIN_100.",
            })

        client = s4 or S4Client()
        # Build filter
        filter_parts = [f"Material eq '{material_number}'"]
        if plant:
            filter_parts.append(f"Plant eq '{plant}'")
        if bom_category:
            filter_parts.append(f"BillOfMaterialCategory eq '{bom_category}'")
        if bom_usage:
            filter_parts.append(f"BOMUsage eq '{bom_usage}'")

        params = {
            "$filter": " and ".join(filter_parts),
            "$select": (
                "BillOfMaterial,BillOfMaterialCategory,BillOfMaterialVariant,"
                "BillOfMaterialVersion,Plant,Material"
            ),
            "$top": MAX_PAGE_SIZE,
            "$format": "json",
        }

        # Try base service first (confirmed working), then _0002 as fallback
        result = await client.get(f"{BOM_ROOT_ALT}/A_BillOfMaterial", params=params)
        if result.get("error"):
            result = await client.get(f"{BOM_ROOT}/A_BillOfMaterial", params=params)

        if result.get("error"):
            return json.dumps({
                "error": result.get("message", "BOM service error"),
                "materialNumber": material_number,
                "hint": "Material may not have a BOM. Use CS01/CS03 in SAP to check.",
            })

        items = result.get("results", [])
        usage_names = {"1": "Production", "2": "Engineering", "3": "Universal",
                       "4": "Plant Maintenance", "5": "Costing", "6": "Sales"}
        bom_headers = []
        for i in items:
            usage_code = i.get("BOMUsage", "") or i.get("BillOfMaterialUsage", "")
            bom_headers.append({
                "billOfMaterial": i.get("BillOfMaterial", ""),
                "billOfMaterialCategory": i.get("BillOfMaterialCategory", ""),
                "billOfMaterialVariant": i.get("BillOfMaterialVariant", ""),
                "billOfMaterialVersion": i.get("BillOfMaterialVersion", ""),
                "plant": i.get("Plant", ""),
                "material": i.get("Material", material_number),
                "bomUsage": usage_code,
                "bomUsageName": usage_names.get(usage_code, usage_code),
            })

        return json.dumps({
            "materialNumber": material_number,
            "plant": plant or "ALL",
            "bomCount": len(bom_headers),
            "bomHeaders": bom_headers,
            "status": "success" if bom_headers else "no_bom_found",
            "note": f"Found {len(bom_headers)} BOM(s) for {material_number}" if bom_headers else f"No BOM found for {material_number}. Material may be purchased externally (no BOM needed).",
        })
    except Exception as e:
        return json.dumps({"error": str(e), "materialNumber": material_number})


def _normalize_bom_number(bom_number: str) -> list[str]:
    """
    Return ALL candidate BOM number formats SAP might store.
    SAP STLNR (BOM number) is an 8-character field in MAST/STKO.
    The OData API A_BillOfMaterial exposes BillOfMaterial as a string key.
    
    We try: raw input, 8-digit pad (canonical), then longer pads as fallback.
    Examples:
      "23"        → ["00000023", "23", "0000000023"]
      "00000023"  → ["00000023", "23", "0000000023"]   (already 8-digit)
      "105"       → ["00000105", "105", "0000000105"]
      "00000105"  → ["00000105", "105", "0000000105"]
    """
    raw = bom_number.strip()
    stripped = raw.lstrip("0") or "0"          # remove leading zeros (keep at least "0")
    padded_8  = stripped.zfill(8)              # canonical: 8-digit (SAP STLNR length)
    padded_10 = stripped.zfill(10)             # 10-digit fallback
    candidates = []
    for v in [padded_8, raw, padded_10]:
        if v not in candidates:
            candidates.append(v)
    return candidates


async def get_material_by_bom_number(
    bom_number: str,
    plant: str | None = None,
    s4: S4Client | None = None,
) -> str:
    """
    REVERSE BOM lookup: given a BOM number, find which material(s) it belongs to.
    This is the CORRECT tool when user asks 'which material has BOM 00000052?'
    or 'material against BOM 00000023'.
    Queries A_BillOfMaterial filtering on BillOfMaterial field (the BOM number).
    Tries 8-digit, raw, and 10-digit formats to match SAP's stored value.
    """
    try:
        # Try ALL candidate formats — SAP stores BOM numbers as 8-char strings
        candidates = _normalize_bom_number(bom_number)
        padded = candidates[0]  # 8-digit is canonical

        if os.environ.get("IBD_TESTING") == "1":
            # ── Confirmed reverse BOM lookup — source: SAP CIN_100 MAST/STKO table ──
            # ONLY entries confirmed from actual SAP data. Nothing invented.
            mock_bom_map = {
                # DEMOCAR — confirmed 07/22-23/2026 by I059550
                "00000380": [{"material": "DEMOCAR", "plant": "1010", "category": "M", "variant": "1"}],
                "00000381": [{"material": "DEMOCAR", "plant": "1710", "category": "M", "variant": "1"}],
                "00000385": [{"material": "DEMOCAR", "plant": "1720", "category": "M", "variant": "1"}],
                # F-10A — confirmed 10/2017 by BPINST (1010 and 1710 only)
                "00000052": [{"material": "F-10A",   "plant": "1010", "category": "M", "variant": "1"}],
                "00000023": [{"material": "F-10A",   "plant": "1710", "category": "M", "variant": "1"}],
                # F-10B — confirmed 10/2017 by BPINST (1010 and 1710 only)
                "00000053": [{"material": "F-10B",   "plant": "1010", "category": "M", "variant": "1"}],
                "00000024": [{"material": "F-10B",   "plant": "1710", "category": "M", "variant": "1"}],
                # F-20A — confirmed 09/2018-09/2019 by BPINST (1010 and 1710 only)
                "00000105": [{"material": "F-20A",   "plant": "1010", "category": "M", "variant": "1"}],
                "00000060": [{"material": "F-20A",   "plant": "1710", "category": "M", "variant": "1"}],
            }
            # Try all candidate formats
            results = []
            for c in candidates:
                results = mock_bom_map.get(c, [])
                if results:
                    padded = c
                    break
            if plant and results:
                results = [r for r in results if r.get("plant") in (plant, "ALL")] or results
            return json.dumps({
                "bomNumber": bom_number,
                "normalizedAs": padded,
                "plant": plant or "ALL",
                "resultCount": len(results),
                "materials": results,
                "status": "success" if results else "not_found",
                "note": f"BOM {bom_number} belongs to: {', '.join(r['material'] for r in results)}" if results else f"No material found for BOM number {bom_number}",
            })

        client = s4 or S4Client()

        # Try ALL candidate formats — SAP may store 8-digit or longer
        all_items = []
        used_format = padded
        for candidate in candidates:
            filter_parts = [f"BillOfMaterial eq '{candidate}'"]
            if plant:
                filter_parts.append(f"Plant eq '{plant}'")
            params = {
                "$filter": " and ".join(filter_parts),
                "$select": "BillOfMaterial,BillOfMaterialCategory,BillOfMaterialVariant,Plant,Material",
                "$top": MAX_PAGE_SIZE,
                "$format": "json",
            }
            result = await client.get(f"{BOM_ROOT}/A_BillOfMaterial", params=params)
            if result.get("error"):
                result = await client.get(f"{BOM_ROOT_ALT}/A_BillOfMaterial", params=params)
            items = result.get("results", []) if not result.get("error") else []
            if items:
                all_items = items
                used_format = candidate
                logger.info("BOM reverse lookup matched with format '%s' for input '%s'", candidate, bom_number)
                break

        if not all_items and result.get("error"):
            return json.dumps({
                "error": result.get("message", "BOM service error"),
                "bomNumber": bom_number,
                "triedFormats": candidates,
                "hint": "BOM number may not exist. Verify in SAP with CS03.",
            })

        materials = [
            {
                "material": i.get("Material", ""),
                "plant": i.get("Plant", ""),
                "category": i.get("BillOfMaterialCategory", ""),
                "variant": i.get("BillOfMaterialVariant", ""),
                "bomNumber": i.get("BillOfMaterial", ""),
            }
            for i in all_items
        ]

        return json.dumps({
            "bomNumber": bom_number,
            "normalizedAs": used_format,
            "triedFormats": candidates,
            "plant": plant or "ALL",
            "resultCount": len(materials),
            "materials": materials,
            "status": "success" if materials else "not_found",
            "note": f"BOM {bom_number} belongs to material(s): {', '.join(set(m['material'] for m in materials))}" if materials else f"No material found for BOM number {bom_number}. Tried formats: {candidates}",
        })

    except Exception as e:
        return json.dumps({"error": str(e), "bomNumber": bom_number})


async def get_bom_explosion(
    material_number: str,
    plant: str,
    bom_usage: str = "1",
    explosion_type: str = "multi",
    s4: S4Client | None = None,
) -> str:
    """
    Explode BOM for a material at a plant — returns all components.
    Uses $expand on the BOM header as primary strategy (most reliable).
    Falls back to navigation property and $filter approaches.
    """
    try:
        if os.environ.get("IBD_TESTING") == "1":
            return json.dumps({
                "materialNumber": material_number,
                "plant": plant,
                "bomUsage": bom_usage,
                "explosionType": explosion_type,
                "componentCount": 3,
                "components": [
                    {"level": 1, "componentMaterial": f"{material_number}-A", "componentDescription": "Assembly A",
                     "quantity": 2, "unit": "EA", "itemCategory": "L", "plant": plant},
                    {"level": 1, "componentMaterial": f"{material_number}-B", "componentDescription": "Raw Material B",
                     "quantity": 5, "unit": "KG", "itemCategory": "L", "plant": plant},
                    {"level": 2, "componentMaterial": f"{material_number}-B1", "componentDescription": "Sub-component B1",
                     "quantity": 1, "unit": "EA", "itemCategory": "L", "plant": plant},
                ],
                "status": "success",
            })

        client = s4 or S4Client()

        # ── Step 1: Fetch BOM header WITH $expand to get items in one call ────
        # Use ONLY the base service (API_BILL_OF_MATERIAL_SRV) — _0002 does not exist
        # on this SAP system. Use $expand=to_BillOfMaterialItem for one-shot retrieval.
        items: list = []
        last_error = ""
        bom_no = ""
        bom_cat = "M"
        bom_var = ""

        def _get_results(res: dict) -> list:
            """Extract OData results from any response shape."""
            if not isinstance(res, dict):
                return []
            # Standard unwrapped shape: {"results": [...]}
            if "results" in res:
                return res["results"] if isinstance(res["results"], list) else []
            # Raw d-envelope shape (shouldn't happen after _unwrap but just in case)
            if "d" in res and isinstance(res.get("d"), dict):
                return res["d"].get("results", [])
            return []

        def _is_real_error(res: dict) -> bool:
            """True only if SAP returned a genuine error (no usable data)."""
            if not isinstance(res, dict):
                return True
            # Client sets error=True on HTTP 4xx/5xx with no parseable OData
            if res.get("error") is True:
                return True
            # Has results → not an error
            if "results" in res or "d" in res:
                return False
            return False

        # ── Strategy A: Header-only query (small response), then item query ─────
        # We do NOT use $expand because the combined response exceeds the proxy's
        # 2775-character buffer limit and gets truncated. Two small queries instead.
        for usage_try in ([bom_usage, None] if bom_usage else [None]):
            filter_parts = [f"Material eq '{material_number}'", f"Plant eq '{plant}'"]
            if usage_try:
                filter_parts.append(f"BOMUsage eq '{usage_try}'")

            hdr_params = {
                "$filter": " and ".join(filter_parts),
                "$select": "BillOfMaterial,BillOfMaterialCategory,BillOfMaterialVariant,BillOfMaterialVersion,Plant,Material,BillOfMaterialHeaderUUID",
                "$top": "5",
                "$format": "json",
            }
            for svc in (BOM_ROOT_ALT, BOM_ROOT):
                res = await client.get(f"{svc}/A_BillOfMaterial", params=hdr_params)
                err = _is_real_error(res)
                hdrs = _get_results(res)
                logger.info("[bom_explosion] HDR svc=%s usage=%s err=%s hdrs=%d",
                            svc, usage_try, err, len(hdrs))
                if not err and hdrs:
                    for hdr in hdrs:
                        bom_no  = hdr.get("BillOfMaterial", "")
                        bom_cat = hdr.get("BillOfMaterialCategory", "M")
                        bom_var = hdr.get("BillOfMaterialVariant", "")
                        hdr_uuid = hdr.get("BillOfMaterialHeaderUUID", "")
                        logger.info("[bom_explosion] found header bom=%s var=%s uuid=%s",
                                    bom_no, bom_var, hdr_uuid)
                        if bom_no:
                            break
                    if bom_no:
                        break
                else:
                    if err:
                        last_error = str(res.get("message", ""))[:200]
            if bom_no:
                break

        # ── Strategy B: Query A_BillOfMaterialItem by BOM number ─────────────
        # Use targeted $filter queries — each returns only a few fields (small response)
        if bom_no and not items:
            item_select = (
                "BillOfMaterialItemUUID,BillOfMaterial,BillOfMaterialCategory,"
                "BillOfMaterialVariant,BillOfMaterialVersion,BillOfMaterialComponent,"
                "BillOfMaterialItemQuantity,BillOfMaterialItemUnit,"
                "BillOfMaterialItemCategory,ComponentDescription,ComponentScrapInPercent,"
                "BillOfMaterialItemNumber"
            )
            var_raw = bom_var or "1"
            var_candidates = list(dict.fromkeys([var_raw, var_raw.zfill(2),
                                                  var_raw.lstrip("0") or "1", "1", "01"]))
            filter_candidates = []
            for var in var_candidates:
                filter_candidates.append(
                    f"BillOfMaterial eq '{bom_no}' and "
                    f"BillOfMaterialCategory eq '{bom_cat}' and "
                    f"BillOfMaterialVariant eq '{var}'"
                )
            filter_candidates.append(
                f"BillOfMaterial eq '{bom_no}' and BillOfMaterialCategory eq '{bom_cat}'"
            )
            filter_candidates.append(f"BillOfMaterial eq '{bom_no}'")

            for filt in filter_candidates:
                for svc in (BOM_ROOT_ALT, BOM_ROOT):
                    res = await client.get(f"{svc}/A_BillOfMaterialItem", params={
                        "$filter": filt,
                        "$select": item_select,
                        "$top": MAX_PAGE_SIZE, "$format": "json"
                    })
                    found = _get_results(res) if not _is_real_error(res) else []
                    logger.info("[bom_explosion] ITEM svc=%s filt=%s found=%d err=%s",
                                svc, filt[:60], len(found), _is_real_error(res))
                    if found:
                        items = found
                        break
                    if _is_real_error(res):
                        last_error = str(res.get("message", ""))[:200]
                if items:
                    break

            # Strategy B2: Navigation via UUID (most direct — no key guessing)
            if not items and hdr_uuid:
                nav_path = f"/A_BillOfMaterial(guid'{hdr_uuid}')/to_BillOfMaterialItem"
                for svc in (BOM_ROOT_ALT, BOM_ROOT):
                    res = await client.get(f"{svc}{nav_path}", params={
                        "$select": item_select, "$top": MAX_PAGE_SIZE, "$format": "json"
                    })
                    found = _get_results(res) if not _is_real_error(res) else []
                    logger.info("[bom_explosion] UUID-NAV svc=%s uuid=%s found=%d", svc, hdr_uuid[:8], len(found))
                    if found:
                        items = found
                        break

        # Strategy B covers all filter cases — no separate Strategy C needed

        # ── No header found at all ─────────────────────────────────────────────
        if not bom_no:
            return json.dumps({
                "error": "No BOM found for this material/plant combination",
                "materialNumber": material_number, "plant": plant,
                "hint": "Check CS03 in SAP. Material may be externally procured.",
            })

        # ── Step 4: Build component list ──────────────────────────────────────
        # Field names from API_BILL_OF_MATERIAL_SRV (confirmed via $metadata):
        #   component  → BillOfMaterialComponent  (not BOMItemComponent)
        #   qty        → BillOfMaterialItemQuantity (not BOMItemQuantity)
        #   unit       → BillOfMaterialItemUnit    (not BOMItemQuantityUnit)
        #   desc       → ComponentDescription
        #   category   → BillOfMaterialItemCategory
        components = []
        for i in items:
            comp_mat = (
                i.get("BillOfMaterialComponent") or   # base service field name (confirmed)
                i.get("BOMItemComponent") or           # _0002 service field name
                i.get("Component") or
                i.get("ComponentMaterial") or
                ""
            )
            qty_raw = (
                i.get("BillOfMaterialItemQuantity") or  # base service
                i.get("BOMItemQuantity") or              # _0002 service
                i.get("ComponentQuantity") or
                0
            )
            unit = (
                i.get("BillOfMaterialItemUnit") or   # base service
                i.get("BOMItemQuantityUnit") or       # _0002 service
                i.get("QuantityUnit") or
                ""
            )
            desc = (
                i.get("ComponentDescription") or
                i.get("BOMItemDescription") or
                ""
            )
            cat = (
                i.get("BillOfMaterialItemCategory") or
                i.get("BOMItemCategoryDescription") or
                i.get("BOMItemCategory") or
                ""
            )
            try:
                qty = float(qty_raw)
            except (TypeError, ValueError):
                qty = 0.0
            components.append({
                "level": 1,
                "componentMaterial": comp_mat,
                "componentDescription": desc,
                "quantity": qty,
                "unit": unit,
                "itemCategory": cat,
                "scrapPct": float(i.get("ComponentScrapInPercent") or 0),
                "bomNumber": bom_no,
                "bomVariant": i.get("BillOfMaterialVariant") or bom_var,
            })

        status = "success" if components else "no_components"
        note = (
            f"Found {len(components)} component(s) for {material_number} at plant {plant} (BOM {bom_no})"
            if components else (
                f"BOM header {bom_no} exists at plant {plant} but the items API returned no components. "
                f"Last API error: {last_error or 'none'}. "
                f"Verify in SAP CS03: Material={material_number}, Plant={plant}, BOM usage=1."
            )
        )

        return json.dumps({
            "materialNumber": material_number,
            "plant": plant,
            "bomNumber": bom_no,
            "bomUsage": bom_usage,
            "explosionType": explosion_type,
            "componentCount": len(components),
            "components": components,
            "status": status,
            "note": note,
        })
    except Exception as e:
        return json.dumps({"error": str(e), "materialNumber": material_number, "plant": plant})


async def get_bom_cross_plant(
    vehicle_material: str,
    plant1: str,
    plant2: str,
    usage: str | None = None,
    alternative: str | None = None,
    s4: S4Client | None = None,
) -> str:
    """
    Retrieve BOM components for a vehicle material across two plants from S/4HANA.

    Returns a JSON summary with component costs per plant and computed cost variances.
    Uses API_BILL_OF_MATERIAL_SRV_0002.
    """
    try:
        logger.info(f"[M3.achieved: cross-plant BOM retrieval started for {vehicle_material}: {plant1} vs {plant2}]")

        if os.environ.get("IBD_TESTING") == "1":
            mock_components = [
                {
                    "materialNumber": f"{vehicle_material}-ENG-001",
                    "materialDescription": "Engine Block Assembly",
                    "componentQty": 1,
                    "costPlant1": 45000.0,
                    "costPlant2": 51750.0,
                    "costVarianceAbsoluteINR": 6750.0,
                    "costVariancePercent": 15.0,
                    "annualVolume": 12000,
                    "totalImpactINR": 81000000.0,
                    "plant1": plant1,
                    "plant2": plant2,
                },
                {
                    "materialNumber": f"{vehicle_material}-TRANS-002",
                    "materialDescription": "Transmission Unit",
                    "componentQty": 1,
                    "costPlant1": 38000.0,
                    "costPlant2": 39900.0,
                    "costVarianceAbsoluteINR": 1900.0,
                    "costVariancePercent": 4.76,
                    "annualVolume": 12000,
                    "totalImpactINR": 22800000.0,
                    "plant1": plant1,
                    "plant2": plant2,
                },
                {
                    "materialNumber": f"{vehicle_material}-BRAKE-003",
                    "materialDescription": "Brake Caliper Set",
                    "componentQty": 4,
                    "costPlant1": 3200.0,
                    "costPlant2": 4160.0,
                    "costVarianceAbsoluteINR": 960.0,
                    "costVariancePercent": 30.0,
                    "annualVolume": 48000,
                    "totalImpactINR": 46080000.0,
                    "plant1": plant1,
                    "plant2": plant2,
                },
            ]
            return json.dumps({
                "vehicleMaterial": vehicle_material,
                "plant1": plant1,
                "plant2": plant2,
                "componentCount": len(mock_components),
                "components": mock_components,
                "status": "success",
            })

        # Production: query S/4HANA directly
        client = s4 or S4Client()
        filter_expr = (
            f"Material eq '{vehicle_material}' and "
            f"(Plant eq '{plant1}' or Plant eq '{plant2}')"
        )
        if usage:
            filter_expr += f" and BillOfMaterialCategory eq '{usage}'"

        params = {
            "$filter": filter_expr,
            # Use minimal safe select; field names may vary between _0002 and base service
            "$select": (
                "BillOfMaterial,BillOfMaterialCategory,BillOfMaterialVariant,"
                "BillOfMaterialVersion,BillOfMaterialItemNodeNumber,"
                "Material,BOMItemQuantity,BOMItemQuantityUnit"
            ),
            "$top": MAX_PAGE_SIZE,
        }
        _ensure_format(params)
        # Try _0002 service first; also probe metadata to find correct entity name
        bom_entity = "MaterialBOMItem"
        bom_service = BOM_ROOT
        try:
            bom_entities = await client.get_service_entities(BOM_ROOT)
            logger.info("BOM _0002 $metadata entity sets: %s", bom_entities)
            for name in bom_entities:
                if "materialbom" in name.lower() or "bom" in name.lower():
                    bom_entity = name
                    bom_service = BOM_ROOT
                    break
        except Exception as bom_meta_err:
            logger.warning("BOM _0002 metadata probe failed: %s", bom_meta_err)

        result = await client.get(f"{bom_service}/{bom_entity}", params=params)

        # Fallback: try base service without _0002 suffix + discover entity name
        if result.get("error"):
            err_msg = str(result.get("message", ""))
            status = result.get("status_code", 0)
            if status in (403, 404) or "/IWFND/MED/170" in err_msg or "No service found" in err_msg or "not found" in err_msg.lower():
                # Probe base service metadata for entity name
                alt_entity = "MaterialBOMItem"
                try:
                    alt_entities = await client.get_service_entities(BOM_ROOT_ALT)
                    logger.info("BOM base $metadata entity sets: %s", alt_entities)
                    # Prefer item-level entity over header/category
                    preferred = ["A_BillOfMaterialItem", "MaterialBOMItem", "A_MaterialBOMItem"]
                    for pref in preferred:
                        if pref in alt_entities:
                            alt_entity = pref
                            break
                    else:
                        for name in alt_entities:
                            if "item" in name.lower() and "bom" in name.lower():
                                alt_entity = name
                                break
                except Exception:
                    pass
                # Also update params to use the entity's correct field names
                # A_BillOfMaterialItem uses BOMItemComponent for material, not Material
                # Switch to A_BillOfMaterial (header) which uses Material + Plant directly
                if alt_entity == "A_BillOfMaterialItem":
                    alt_entity = "A_BillOfMaterial"
                    alt_params = {
                        "$filter": (
                            f"Material eq '{vehicle_material}' and "
                            f"(Plant eq '{plant1}' or Plant eq '{plant2}')"
                        ),
                        "$select": (
                            "BillOfMaterial,BillOfMaterialCategory,BillOfMaterialVariant,"
                            "Plant,Material"
                        ),
                        "$top": MAX_PAGE_SIZE,
                        "$format": "json",
                    }
                    result = await client.get(f"{BOM_ROOT_ALT}/{alt_entity}", params=alt_params)
                else:
                    logger.info("BOM fallback: %s / %s", BOM_ROOT_ALT, alt_entity)
                    result = await client.get(f"{BOM_ROOT_ALT}/{alt_entity}", params=params)

        if result.get("error"):
            logger.error(f"[M3.missed: BOM retrieval error for {vehicle_material}: {result}]")
            return json.dumps({"error": result.get("message", "Unknown error"), "vehicleMaterial": vehicle_material})

        items = result.get("results", [])
        return json.dumps({
            "vehicleMaterial": vehicle_material,
            "plant1": plant1,
            "plant2": plant2,
            "componentCount": len(items),
            "components": items,
            "status": "success",
        })

    except Exception as e:
        logger.error(f"[M3.missed: cross-plant BOM retrieval failed for {vehicle_material}, reason={e}]")
        return json.dumps({"error": str(e), "vehicleMaterial": vehicle_material})


async def get_material_procurement_type(
    material_number: str,
    plant: str,
    s4: S4Client | None = None,
) -> str:
    """
    Retrieve material master procurement type for a material at a plant.

    Returns procurement type (E=In-house, F=External, X=Both).
    Uses API_PRODUCT_SRV.
    """
    try:
        if os.environ.get("IBD_TESTING") == "1":
            # ── NO mock procurement types — must come from live SAP CIN_100 only ──
            # Procurement type must be fetched from A_ProductPlant in SAP.
            return json.dumps({
                "materialNumber": material_number,
                "plant": plant,
                "procurementType": "",
                "procurementTypeName": "Unknown — fetch from live SAP",
                "mrpType": "",
                "status": "no_mock_data",
                "note": f"Procurement type for {material_number} at plant {plant} must be fetched live from SAP CIN_100 "
                        f"(API: A_ProductPlant). No hardcoded mock data is used.",
            })

        client = s4 or S4Client()
        params = {
            "$filter": f"Product eq '{material_number}' and Plant eq '{plant}'",
            "$select": "Product,Plant,ProcurementType,MRPType",
            "$top": 1,
        }
        _ensure_format(params)
        result = await client.get(f"{PRODUCT_ROOT}/A_ProductPlant", params=params)
        if result.get("error"):
            return json.dumps({"error": result.get("message"), "materialNumber": material_number})

        items = result.get("results", [])
        if not items:
            return json.dumps({"materialNumber": material_number, "plant": plant,
                                "error": "not_found",
                                "procurementType": "Unknown",
                                "message": f"Material '{material_number}' not found in plant {plant}"})

        item = items[0]
        ptype = item.get("ProcurementType", "")
        type_names = {"E": "In-house", "F": "External", "X": "Both"}
        return json.dumps({
            "materialNumber": material_number,
            "plant": plant,
            "procurementType": ptype,
            "procurementTypeName": type_names.get(ptype, "Unknown"),
            "mrpType": item.get("MRPType", ""),
        })

    except Exception as e:
        return json.dumps({"error": str(e), "materialNumber": material_number, "plant": plant})


async def get_purchase_info_records(
    material_number: str,
    plant: str,
    purchasing_org: str | None = None,
    s4: S4Client | None = None,
) -> str:
    """
    Retrieve purchase info records (vendor prices, conditions) for a material.

    Used to detect single-supplier dependency. Uses API_INFORECORD_PROCESS_SRV.
    """
    try:
        if os.environ.get("IBD_TESTING") == "1":
            # ── NO mock vendor/supplier data — must come from live SAP only ──────
            # Vendor codes (V001, V002 etc.) were invented and do not exist in CIN_100.
            # DEMOCAR procurement type is E (in-house) — no purchase info records expected.
            # Return empty result honestly.
            return json.dumps({
                "materialNumber": material_number,
                "plant": plant,
                "purchasingOrg": purchasing_org or "",
                "vendorCount": 0,
                "hasMultipleSuppliers": False,
                "infoRecords": [],
                "status": "no_data",
                "note": f"No purchase info records found for {material_number} at plant {plant} in SAP CIN_100. "
                        f"Material may be produced in-house (procurement type E) — info records only exist for externally purchased components.",
            })

        client = s4 or S4Client()

        # Discover the correct entity set name for info records via $metadata
        pir_entity = "A_PurchasingInfoRecord"
        try:
            pir_entities = await client.get_service_entities(PIR_ROOT)
            logger.info("PIR $metadata entity sets: %s", pir_entities)
            for name in pir_entities:
                if "inforecord" in name.lower() or "purchasinginfo" in name.lower() or "pir" in name.lower():
                    pir_entity = name
                    logger.info("PIR entity resolved to: %s", pir_entity)
                    break
            if pir_entity == "A_PurchasingInfoRecord" and pir_entities:
                # If we didn't find a matching name, use whatever the service exposes
                pir_entity = pir_entities[0]
                logger.info("PIR entity fallback to first entity set: %s", pir_entity)
        except Exception as pir_meta_err:
            logger.warning("PIR metadata probe failed: %s", pir_meta_err)

        # On this on-premise system, only Material is reliably filterable
        params = {
            "$filter": f"Material eq '{material_number}'",
            # Minimal safe select — use only key fields to avoid field-not-found errors
            "$select": "PurchasingInfoRecord,Supplier,Material",
            "$top": MAX_PAGE_SIZE,
        }
        _ensure_format(params)
        result = await client.get(f"{PIR_ROOT}/{pir_entity}", params=params)

        # If entity name was wrong (404/not found), try without $select
        if result.get("error"):
            err_msg = str(result.get("message", ""))
            if result.get("status_code") == 404 or "not found" in err_msg.lower():
                params_no_select = {"$filter": f"Material eq '{material_number}'",
                                     "$top": MAX_PAGE_SIZE, "$format": "json"}
                result = await client.get(f"{PIR_ROOT}/{pir_entity}", params=params_no_select)
        if result.get("error"):
            return json.dumps({"error": result.get("message"), "materialNumber": material_number})

        items = result.get("results", [])
        vendors = [
            {
                "vendorCode": i.get("Supplier", ""),
                "infoRecord": i.get("PurchasingInfoRecord", ""),
            }
            for i in items
        ]
        return json.dumps({
            "materialNumber": material_number,
            "plant": plant,
            "purchasingOrg": purchasing_org or "",
            "vendorCount": len(vendors),
            "hasMultipleSuppliers": len(vendors) > 1,
            "infoRecords": vendors,
        })

    except Exception as e:
        return json.dumps({"error": str(e), "materialNumber": material_number})


async def get_purchase_order_history(
    material_number: str,
    plant: str,
    date_from: str | None = None,
    date_to: str | None = None,
    s4: S4Client | None = None,
) -> str:
    """
    Retrieve purchase order history for a material at a plant.

    Returns PO lines with quantities and prices. Uses API_PURCHASEORDER_PROCESS_SRV.
    """
    try:
        if os.environ.get("IBD_TESTING") == "1":
            # ── NO mock PO data — PO data must come from live SAP only ──────────
            # Confirmed from SAP: POs 4500001234/1456/1789 belong to
            # Y200 Bike (MZ-TG-Y200), RM233-2, BKR-300 parts — NOT DEMOCAR.
            # DEMOCAR has NO purchase orders in CIN_100.
            # Return empty result honestly — never invent PO numbers.
            return json.dumps({
                "materialNumber": material_number,
                "plant": plant,
                "dateFrom": date_from or "",
                "dateTo": date_to or "",
                "totalOrders": 0,
                "avgPrice": 0.0,
                "currency": "",
                "orders": [],
                "status": "no_data",
                "note": f"No purchase orders found for {material_number} at plant {plant} in SAP CIN_100. "
                        f"This material may be produced in-house (procurement type E) and not externally purchased.",
            })

        client = s4 or S4Client()
        filter_parts = [f"Material eq '{material_number}'", f"Plant eq '{plant}'"]
        if date_from:
            filter_parts.append(f"DocumentDate ge datetime'{date_from}T00:00:00'")
        if date_to:
            filter_parts.append(f"DocumentDate le datetime'{date_to}T23:59:59'")

        params = {
            "$filter": " and ".join(filter_parts),
            "$select": (
                "PurchaseOrder,PurchaseOrderItem,Plant,Material,PurchaseOrderItemText,"
                "OrderQuantity,PurchaseOrderQuantityUnit,NetPriceAmount,"
                "GoodsReceiptIsExpected,InvoiceIsExpected"
            ),
            "$top": MAX_PAGE_SIZE,
        }
        _ensure_format(params)
        result = await client.get(f"{PO_ROOT}/A_PurchaseOrderItem", params=params)
        if result.get("error"):
            return json.dumps({"error": result.get("message"), "materialNumber": material_number})

        items = result.get("results", [])
        orders = [
            {
                "poNumber": i.get("PurchaseOrder", ""),
                "poItem": i.get("PurchaseOrderItem", ""),
                "quantity": i.get("OrderQuantity", 0),
                "netPrice": i.get("NetPriceAmount", 0),
            }
            for i in items
        ]
        prices = [float(o["netPrice"]) for o in orders if o["netPrice"]]
        avg = round(sum(prices) / len(prices), 2) if prices else 0.0
        return json.dumps({
            "materialNumber": material_number,
            "plant": plant,
            "dateFrom": date_from or "",
            "dateTo": date_to or "",
            "totalOrders": len(orders),
            "avgPrice": avg,
            "orders": orders,
        })

    except Exception as e:
        return json.dumps({"error": str(e), "materialNumber": material_number})


async def get_material_valuation(
    material_number: str,
    plant: str,
    valuation_area: str | None = None,
    s4: S4Client | None = None,
) -> str:
    """
    Retrieve material valuation: standard price, moving average price, currency, price control.
    Tries 4 strategies in order until data is found:
      S1: API_PRODUCT_SRV/A_ProductValuation  (Product eq + ValuationArea eq)
      S2: API_PRODUCT_SRV/A_ProductValuation  (Material eq  — ECC legacy field name)
      S3: API_MATERIAL_VALUATION_SRV/A_MaterialValuation (direct MBEW table)
      S4: API_PRODUCT_SRV/A_ProductPlant      (procurement type only fallback)
    """
    try:
        if os.environ.get("IBD_TESTING") == "1":
            return json.dumps({
                "materialNumber": material_number, "plant": plant,
                "valuationArea": valuation_area or plant,
                "standardPrice": 0.0, "movingAveragePrice": 0.0, "effectivePrice": 0.0,
                "priceControl": "", "currency": "", "valuationClass": "",
                "status": "no_mock_data",
                "note": f"Price data for {material_number}/{plant} must be fetched live from SAP CIN_100.",
            })

        client = s4 or S4Client()
        va = valuation_area or plant
        item = None

        # ── Strategy 1: A_ProductValuation with Product key ──────────────────
        r1 = await client.get(f"{PRODUCT_ROOT}/A_ProductValuation", params={
            "$filter": f"Product eq '{material_number}' and ValuationArea eq '{va}'",
            "$top": "1", "$format": "json",
        })
        items1 = r1.get("results", []) if not r1.get("error") else []
        logger.info("[valuation] S1 A_ProductValuation(Product) %s/%s → %d items err=%s",
                    material_number, va, len(items1), r1.get("error"))
        if items1:
            item = items1[0]

        # ── Strategy 2: A_ProductValuation with Material key (ECC) ──────────
        if not item:
            r2 = await client.get(f"{PRODUCT_ROOT}/A_ProductValuation", params={
                "$filter": f"Material eq '{material_number}' and ValuationArea eq '{va}'",
                "$top": "1", "$format": "json",
            })
            items2 = r2.get("results", []) if not r2.get("error") else []
            logger.info("[valuation] S2 A_ProductValuation(Material) %s/%s → %d items",
                        material_number, va, len(items2))
            if items2:
                item = items2[0]

        # ── Strategy 3: API_MATERIAL_VALUATION_SRV/A_MaterialValuation (MBEW) ─
        if not item:
            for mat_key in (f"Material eq '{material_number}'", f"Product eq '{material_number}'"):
                r3 = await client.get(f"{VALUATION_ROOT}/A_MaterialValuation", params={
                    "$filter": f"{mat_key} and ValuationArea eq '{va}'",
                    "$top": "1", "$format": "json",
                })
                items3 = r3.get("results", []) if not r3.get("error") else []
                logger.info("[valuation] S3 A_MaterialValuation(%s) %s/%s → %d items",
                            mat_key[:8], material_number, va, len(items3))
                if items3:
                    item = items3[0]
                    break

        # ── Strategy 4: A_ProductPlant — procurement/MRP only ────────────────
        if not item:
            r4 = await client.get(f"{PRODUCT_ROOT}/A_ProductPlant", params={
                "$filter": f"Product eq '{material_number}' and Plant eq '{plant}'",
                "$select": "Product,Plant,ProcurementType,MRPType,LotSizeType,ReorderThresholdQuantity",
                "$top": "1", "$format": "json",
            })
            pp = r4.get("results", []) if not r4.get("error") else []
            logger.info("[valuation] S4 A_ProductPlant %s/%s → %d items", material_number, plant, len(pp))
            if pp:
                pi = pp[0]
                return json.dumps({
                    "materialNumber": material_number, "plant": plant, "valuationArea": va,
                    "procurementType": pi.get("ProcurementType", ""),
                    "mrpType": pi.get("MRPType", ""),
                    "lotSize": pi.get("LotSizeType", ""),
                    "reorderPoint": float(pi.get("ReorderThresholdQuantity") or 0),
                    "standardPrice": 0.0, "movingAveragePrice": 0.0,
                    "effectivePrice": 0.0, "currency": "", "priceControl": "",
                    "note": "Procurement type from ProductPlant; no price data found.",
                })
            return json.dumps({
                "error": "no_valuation_data",
                "materialNumber": material_number, "plant": plant,
                "note": f"No valuation data found for {material_number} at plant {plant} in any API.",
            })

        logger.info("[valuation] item keys: %s", list(item.keys())[:20])

        # ── Extract fields — try all known SAP field name variants ───────────
        # PriceControl: SAP API uses "PriceControl" (S/V), older APIs use other names
        pc = (item.get("PriceControl") or          # standard S/4H field (CONFIRMED correct)
               item.get("PriceDeterminationControl") or
               item.get("PriceControlIndicator") or
               item.get("InventoryValuationProcedure") or "")

        std = float(item.get("StandardPrice") or
                    item.get("StdCost") or 0)

        mav = float(item.get("MovingAveragePrice") or
                    item.get("MovingAvgPrice") or
                    item.get("TotalValue") or 0)

        currency = (item.get("Currency") or
                    item.get("CurrencyCode") or
                    item.get("CompanyCodeCurrency") or "")

        val_class = item.get("ValuationClass") or ""
        val_cat   = item.get("ValuationCategory") or ""

        price_unit = float(item.get("PriceUnitQty") or
                           item.get("PriceUnit") or 1)

        old_std  = float(item.get("OldStandardPrice") or 0)
        last_chg = (item.get("LastStandardPriceChangeDate") or
                    item.get("LastChangeDate") or "")

        total_stock = float(item.get("TotalStock") or
                            item.get("TotalValuedStockQty") or
                            item.get("TotalManagedStockQty") or 0)
        total_val   = float(item.get("TotalValuedStock") or
                            item.get("TotalStockValue") or
                            item.get("TotalValue") or 0)

        # SAP stores price as "price per PriceUnit" (e.g. 4200 EUR per 100 units = 42 EUR/unit).
        # Normalise std and mav to price-per-1-unit before any further computation.
        if price_unit > 1:
            std = round(std / price_unit, 6)
            mav = round(mav / price_unit, 6)
            if old_std:
                old_std = round(old_std / price_unit, 6)
        # Always return priceUnit=1 — prices are already normalised to per-unit above.
        # Keeping the original value (e.g. 100) in the JSON would cause the LLM to
        # multiply effectivePrice × priceUnit a second time, re-inflating the price.
        price_unit = 1

        # Effective price: prefer standard for S/2 control, MAP for V/3
        use_std = pc in ("S", "2")
        if use_std and std > 0:
            effective = std
        elif mav > 0:
            effective = mav
        else:
            effective = std

        pc_label = {
            "S": "Standard Price (S)", "2": "Standard Price (S)",
            "V": "Moving Average (V)", "3": "Moving Average (V)",
        }.get(pc, pc or "—")

        return json.dumps({
            "materialNumber": material_number, "plant": plant, "valuationArea": va,
            "priceControl": pc, "priceControlLabel": pc_label,
            "valuationClass": val_class, "valuationCategory": val_cat,
            "priceUnit": price_unit, "currency": currency,
            "standardPrice": std, "movingAveragePrice": mav, "effectivePrice": effective,
            "oldStandardPrice": old_std, "lastPriceChangeDate": last_chg,
            "totalStock": total_stock, "totalStockValue": total_val,
        })

    except Exception as e:
        return json.dumps({"error": str(e), "materialNumber": material_number})


async def get_bom_items_raw(
    bom_number: str,
    plant: str,
    bom_category: str = "M",
    bom_variant: str = "1",
    s4: S4Client | None = None,
) -> str:
    """
    RAW diagnostic: fetch BOM items directly using every possible strategy.
    Use when get_bom_explosion returns no components — this shows exactly what the API returns.
    Tries $expand, navigation, and $filter on both _0002 and base BOM services.
    Returns raw API responses for debugging.
    """
    try:
        if os.environ.get("IBD_TESTING") == "1":
            return json.dumps({"status": "mock", "bom_number": bom_number})

        client = s4 or S4Client()
        bom_no = bom_number.strip().zfill(8)
        var_candidates = list(dict.fromkeys([bom_variant, bom_variant.zfill(2),
                                              bom_variant.lstrip("0") or "1", "1", "01"]))
        ver_candidates = ["", "0", "00"]
        results = {}

        # Try $expand directly
        for svc in (BOM_ROOT, BOM_ROOT_ALT):
            r = await client.get(f"{svc}/A_BillOfMaterial", params={
                "$filter": f"BillOfMaterial eq '{bom_no}' and BillOfMaterialCategory eq '{bom_category}'",
                "$expand": "to_BillOfMaterialItem",
                "$top": "5", "$format": "json"
            })
            key = f"expand_{svc.split('/')[-1]}"
            results[key] = {
                "error": r.get("error"),
                "count": len(r.get("results", [])),
                "first_item_keys": list(r.get("results", [{}])[0].keys())[:10] if r.get("results") else [],
                "items_in_first": len((r.get("results", [{}])[0].get("to_BillOfMaterialItem") or {}).get("results", [])) if r.get("results") else 0,
            }
            # If we got items, return them immediately
            if r.get("results"):
                for hdr in r["results"]:
                    nav = hdr.get("to_BillOfMaterialItem", {})
                    if isinstance(nav, dict):
                        nav = nav.get("results", [])
                    if nav:
                        return json.dumps({
                            "status": "found_via_expand",
                            "bom_number": bom_no,
                            "service": svc,
                            "component_count": len(nav),
                            "components": [
                                {
                                    "component": i.get("BOMItemComponent", ""),
                                    "qty": i.get("BOMItemQuantity", ""),
                                    "unit": i.get("BOMItemQuantityUnit", ""),
                                    "category": i.get("BOMItemCategoryDescription", ""),
                                }
                                for i in nav
                            ]
                        })

        # Try navigation property
        for svc in (BOM_ROOT, BOM_ROOT_ALT):
            for var in var_candidates:
                for ver in ver_candidates:
                    nav_path = (
                        f"/A_BillOfMaterial("
                        f"BillOfMaterial='{bom_no}',"
                        f"BillOfMaterialCategory='{bom_category}',"
                        f"BillOfMaterialVariant='{var}',"
                        f"BillOfMaterialVersion='{ver}'"
                        f")/to_BillOfMaterialItem"
                    )
                    r = await client.get(f"{svc}{nav_path}", params={"$top": MAX_PAGE_SIZE, "$format": "json"})
                    found = r.get("results", []) if not r.get("error") else []
                    if found:
                        return json.dumps({
                            "status": "found_via_navigation",
                            "bom_number": bom_no, "variant": var, "version": ver, "service": svc,
                            "component_count": len(found),
                            "components": [
                                {
                                    "component": i.get("BOMItemComponent", ""),
                                    "qty": i.get("BOMItemQuantity", ""),
                                    "unit": i.get("BOMItemQuantityUnit", ""),
                                }
                                for i in found
                            ]
                        })
                    key = f"nav_{svc.split('/')[-1]}_var{var}_ver{ver}"
                    results[key] = {"error": str(r.get("message", ""))[:100], "status": r.get("status_code")}

        # Try A_BillOfMaterialItem with $filter
        for svc in (BOM_ROOT, BOM_ROOT_ALT):
            r = await client.get(f"{svc}/A_BillOfMaterialItem", params={
                "$filter": f"BillOfMaterial eq '{bom_no}'",
                "$top": MAX_PAGE_SIZE, "$format": "json"
            })
            found = r.get("results", []) if not r.get("error") else []
            if found:
                return json.dumps({
                    "status": "found_via_filter",
                    "bom_number": bom_no, "service": svc,
                    "component_count": len(found),
                    "components": [
                        {
                            "component": i.get("BOMItemComponent", ""),
                            "qty": i.get("BOMItemQuantity", ""),
                            "unit": i.get("BOMItemQuantityUnit", ""),
                        }
                        for i in found
                    ]
                })
            results[f"filter_{svc.split('/')[-1]}"] = {
                "error": str(r.get("message", ""))[:200], "status": r.get("status_code")
            }

        return json.dumps({
            "status": "not_found",
            "bom_number": bom_no, "plant": plant,
            "all_attempts": results,
            "diagnosis": (
                "All API strategies returned empty. Possible causes: "
                "1) BOM items are deleted/inactive in SAP. "
                "2) API_BILL_OF_MATERIAL_SRV does not expose items for this BOM type. "
                "3) Authorization issue on BOM item entity. "
                "4) BOM items exist in a different BOM usage (try usage 2, 3, or 5). "
                "Please verify in CS03 with plant=" + plant + "."
            )
        })
    except Exception as e:
        return json.dumps({"error": str(e), "bom_number": bom_number})


async def explain_bom_cost_variance(
    material_number: str,
    plant1: str,
    plant2: str,
    max_depth: int = 6,
    variance_threshold_pct: float = 1.0,
    s4: S4Client | None = None,
) -> str:
    """
    Recursively explains WHY cost differs between two plants for a material.

    Works like a forensic accountant:
    - Level 0: Top-level material cost in both plants
    - Level 1: Which BOM components differ? Same qty? Same price?
    - Level 2: For differing components, drill into their sub-BOMs
    - Level N: Continue until leaf components (no BOM) or max_depth reached

    For each differing node explains:
      - Price difference (same / higher / lower, by how much)
      - Root cause: currency, procurement type, supplier price, no BOM at that plant
      - Whether it's Make (in-house) or Buy (external) at each plant
      - Supplier names if available
      - Quantity difference contribution
    """
    try:
        client = s4 or S4Client()

        # ── FX helper ────────────────────────────────────────────────────────
        _fx_cache_local: dict = {}
        async def to_common_currency(amount: float, currency: str) -> float:
            """Convert any amount to USD for comparison."""
            amount = float(amount or 0)
            if not currency or currency == "USD" or amount == 0:
                return amount
            key = f"{currency}_USD"
            if key not in _fx_cache_local:
                raw = await get_tcurr_fx_rate(currency, "USD", "M", None, s4=client)
                d = json.loads(raw)
                _fx_cache_local[key] = float(d.get("rate") or 0) or 1.0
            return round(amount * _fx_cache_local[key], 4)

        # ── Valuation + procurement for one material/plant ───────────────────
        async def get_node_data(mat: str, plant: str) -> dict:
            """Price, currency, procurement type, vendors for one material+plant."""
            try:
                val = json.loads(await get_material_valuation(mat, plant, s4=client))
                proc = json.loads(await get_material_procurement_type(mat, plant, s4=client))
                pir = json.loads(await get_purchase_info_records(mat, plant, s4=client))
                price = float(val.get("effectivePrice") or val.get("standardPrice") or val.get("movingAveragePrice") or 0)
                return {
                    "price": price,
                    "currency": val.get("currency", ""),
                    "priceUSD": await to_common_currency(price, val.get("currency", "")),
                    "priceControl": val.get("priceControlLabel", ""),
                    "procType": proc.get("procurementType", ""),
                    "procTypeName": proc.get("procurementTypeName", "Unknown"),
                    "vendors": [v.get("vendorCode", "") for v in pir.get("infoRecords", [])],
                    "vendorCount": pir.get("vendorCount", 0),
                    "error": val.get("error") or proc.get("error"),
                }
            except Exception as e:
                return {"price": 0, "currency": "", "priceUSD": 0, "error": str(e)}

        # ── Get BOM components for one material/plant ────────────────────────
        async def get_components(mat: str, plant: str) -> list:
            """Returns list of {material, description, quantity, unit} or []."""
            try:
                raw = await get_bom_explosion(mat, plant, "1", "single", s4=client)
                data = json.loads(raw)
                return data.get("components", [])
            except Exception:
                return []

        # ── Recursive explainer ───────────────────────────────────────────────
        explanation_nodes: list = []

        async def recurse(mat: str, qty_p1: float, qty_p2: float,
                          depth: int, parent_label: str, visited: set) -> dict:
            """
            Returns {
              material, depth, parent,
              data_p1, data_p2,
              price_p1_usd, price_p2_usd, total_p1_usd, total_p2_usd,
              diff_usd, diff_pct,
              cause, verdict,   # SAME / HIGHER_P1 / HIGHER_P2 / NO_PRICE_DATA
              children: [...]   # recursive
            }
            """
            if depth > max_depth or mat in visited:
                return {}
            visited = visited | {mat}

            # Get prices at both plants
            d1 = await get_node_data(mat, plant1)
            d2 = await get_node_data(mat, plant2)
            p1_usd = float(d1.get("priceUSD") or 0) * float(qty_p1 or 0)
            p2_usd = float(d2.get("priceUSD") or 0) * float(qty_p2 or 0)
            diff_usd = p2_usd - p1_usd  # positive = p2 more expensive
            max_val = max(abs(p1_usd), abs(p2_usd), 0.01)
            diff_pct = round(abs(diff_usd) / max_val * 100, 2)

            # Determine verdict
            if d1["priceUSD"] == 0 and d2["priceUSD"] == 0:
                verdict = "NO_PRICE_DATA"
            elif diff_pct < variance_threshold_pct:
                verdict = "SAME"
            elif diff_usd > 0:
                verdict = f"HIGHER_AT_{plant2}"
            else:
                verdict = f"HIGHER_AT_{plant1}"

            # Determine root cause
            cause_parts = []
            if d1["currency"] and d2["currency"] and d1["currency"] != d2["currency"]:
                cause_parts.append(
                    f"Currency difference: {plant1} uses {d1['currency']}, {plant2} uses {d2['currency']}"
                )
            if d1["procType"] != d2["procType"]:
                cause_parts.append(
                    f"Procurement type: {plant1}={d1['procTypeName']}, {plant2}={d2['procTypeName']}"
                )
            if d1["procType"] == "F" and d2["procType"] == "F":
                v1_str = ", ".join(d1["vendors"][:3]) or "unknown"
                v2_str = ", ".join(d2["vendors"][:3]) or "unknown"
                if d1["vendors"] != d2["vendors"] or d1["price"] != d2["price"]:
                    cause_parts.append(
                        f"Different supplier pricing: {plant1} buys from [{v1_str}] at {d1['price']} {d1['currency']}, "
                        f"{plant2} from [{v2_str}] at {d2['price']} {d2['currency']}"
                    )
            if qty_p1 != qty_p2:
                cause_parts.append(
                    f"Quantity difference: {plant1} uses {qty_p1}, {plant2} uses {qty_p2}"
                )
            if d1["priceUSD"] == 0 and d2["priceUSD"] > 0:
                cause_parts.append(f"No price maintained at {plant1} — component may not be used there")
            elif d2["priceUSD"] == 0 and d1["priceUSD"] > 0:
                cause_parts.append(f"No price maintained at {plant2} — component may not be used there")
            if not cause_parts and verdict not in ("SAME", "NO_PRICE_DATA"):
                cause_parts.append(
                    f"Price difference: {plant1}={d1['price']} {d1['currency']}, "
                    f"{plant2}={d2['price']} {d2['currency']} — possibly different standard cost runs"
                )

            node = {
                "material": mat,
                "depth": depth,
                "parent": parent_label,
                "qty_p1": qty_p1,
                "qty_p2": qty_p2,
                f"price_{plant1}": d1["price"],
                f"currency_{plant1}": d1["currency"],
                f"priceUSD_{plant1}": d1["priceUSD"],
                f"totalUSD_{plant1}": round(p1_usd, 4),
                f"procType_{plant1}": d1["procTypeName"],
                f"vendors_{plant1}": d1["vendors"],
                f"price_{plant2}": d2["price"],
                f"currency_{plant2}": d2["currency"],
                f"priceUSD_{plant2}": d2["priceUSD"],
                f"totalUSD_{plant2}": round(p2_usd, 4),
                f"procType_{plant2}": d2["procTypeName"],
                f"vendors_{plant2}": d2["vendors"],
                "diffUSD": round(diff_usd, 4),
                "diffPct": diff_pct,
                "verdict": verdict,
                "cause": " | ".join(cause_parts) if cause_parts else "No significant cause identified",
                "hasChildren": False,
                "children": [],
            }

            # Drill deeper if this node has a variance AND has a BOM
            if verdict not in ("SAME", "NO_PRICE_DATA") or depth == 1:
                comps_p1 = await get_components(mat, plant1)
                comps_p2 = await get_components(mat, plant2)

                if comps_p1 or comps_p2:
                    node["hasChildren"] = True
                    # Build qty map keyed by component material
                    qty_map_p1 = {c["componentMaterial"]: float(c.get("quantity", 1))
                                  for c in comps_p1 if c.get("componentMaterial")}
                    qty_map_p2 = {c["componentMaterial"]: float(c.get("quantity", 1))
                                  for c in comps_p2 if c.get("componentMaterial")}
                    desc_map = {c["componentMaterial"]: c.get("componentDescription", "")
                                for c in comps_p1 + comps_p2 if c.get("componentMaterial")}

                    all_comp_mats = sorted(set(qty_map_p1.keys()) | set(qty_map_p2.keys()))
                    children = []
                    for comp_mat in all_comp_mats:
                        c_qty_p1 = qty_map_p1.get(comp_mat, 0) * qty_p1
                        c_qty_p2 = qty_map_p2.get(comp_mat, 0) * qty_p2
                        child = await recurse(comp_mat, c_qty_p1, c_qty_p2,
                                              depth + 1, mat, visited)
                        if child:
                            child["description"] = desc_map.get(comp_mat, "")
                            children.append(child)

                    # Sort children by abs(diffUSD) desc — biggest drivers first
                    children.sort(key=lambda x: abs(x.get("diffUSD", 0)), reverse=True)
                    node["children"] = children

            explanation_nodes.append(node)
            return node

        # ── Run from top level ────────────────────────────────────────────────
        root = await recurse(material_number, 1.0, 1.0, 0, "TOP", set())

        # ── Build flat explanation summary (for easy agent narration) ─────────
        def flatten(node: dict, rows: list, indent: int = 0):
            if not node:
                return
            verdict = node.get("verdict", "")
            diff = node.get("diffUSD", 0)
            pct = node.get("diffPct", 0)
            mat = node.get("material", "")
            desc = node.get("description", "")
            cause = node.get("cause", "")
            label = f"{'  ' * indent}L{node.get('depth', 0)}: {mat}" + (f" ({desc})" if desc else "")
            p1_info = f"{node.get(f'price_{plant1}') or 0} {node.get(f'currency_{plant1}') or ''} [{node.get(f'procType_{plant1}') or ''}]"
            p2_info = f"{node.get(f'price_{plant2}') or 0} {node.get(f'currency_{plant2}') or ''} [{node.get(f'procType_{plant2}') or ''}]"
            rows.append({
                "label": label,
                "material": mat,
                "description": desc,
                "depth": node.get("depth", 0),
                "plant1_info": p1_info,
                "plant2_info": p2_info,
                "diffUSD": round(diff, 2),
                "diffPct": pct,
                "verdict": verdict,
                "cause": cause,
                "hasChildren": node.get("hasChildren", False),
            })
            for child in node.get("children", []):
                flatten(child, rows, indent + 1)

        flat_rows: list = []
        flatten(root, flat_rows)

        # ── Top cost drivers (deepest differing leaf nodes) ──────────────────
        significant = [r for r in flat_rows if r.get("diffPct", 0) >= variance_threshold_pct
                       and r["verdict"] not in ("SAME", "NO_PRICE_DATA")]
        significant.sort(key=lambda x: abs(x.get("diffUSD", 0)), reverse=True)

        # ── Make/Buy recommendation per differing component ──────────────────
        recommendations = []
        for row in significant:
            mat = row["material"]
            # Find original node for vendor info
            orig = next((n for n in explanation_nodes if n.get("material") == mat), {})
            pt1 = orig.get(f"procType_{plant1}", "")
            pt2 = orig.get(f"procType_{plant2}", "")
            v1 = orig.get(f"vendors_{plant1}", [])
            v2 = orig.get(f"vendors_{plant2}", [])
            cheaper_plant = plant1 if (orig.get(f"totalUSD_{plant1}", 0) or 0) <= (orig.get(f"totalUSD_{plant2}", 0) or 0) else plant2
            expensive_plant = plant2 if cheaper_plant == plant1 else plant1

            if "E" in (pt1, pt2) and "F" in (pt1, pt2):
                if pt1 == "F":
                    action = f"Consider in-house at {plant1} (currently buying) — {plant2} makes it cheaper in-house"
                else:
                    action = f"Consider in-house at {plant2} (currently buying) — {plant1} makes it cheaper in-house"
            elif not v1 and not v2 and pt1 == pt2 == "":
                action = f"Check pricing config — no supplier or procurement type set at one plant"
            elif row["diffPct"] >= 20:
                action = f"RENEGOTIATE at {expensive_plant} using {cheaper_plant} price as benchmark"
            elif row["diffPct"] >= 5:
                action = f"CONSOLIDATE procurement — source from {cheaper_plant}'s supplier for both plants"
            else:
                action = "MONITOR — minor variance, no immediate action needed"

            recommendations.append({
                "material": mat,
                "description": row.get("description", "") or "",
                "depth": row.get("depth", 0),
                "cheaperPlant": cheaper_plant,
                "expensivePlant": expensive_plant,
                "diffPct": float(row.get("diffPct") or 0),
                "diffUSD": float(row.get("diffUSD") or 0),
                "action": action,
                "cause": row.get("cause", "") or "",
            })

        return json.dumps({
            "materialNumber": material_number,
            "plant1": plant1,
            "plant2": plant2,
            "rootVariancePct": root.get("diffPct", 0),
            "rootVerdict": root.get("verdict", ""),
            "rootCause": root.get("cause", ""),
            "tree": root,
            "flatExplanation": flat_rows,
            "significantNodes": significant[:15],
            "topCostDrivers": significant[:5],
            "recommendations": recommendations[:10],
            "totalNodesAnalysed": len(explanation_nodes),
            "status": "success",
        })

    except Exception as e:
        logger.exception("[explain_bom_cost_variance] error")
        return json.dumps({"error": str(e), "materialNumber": material_number})


async def deep_bom_cost_analysis(
    material_number: str,
    plants: list | None = None,
    max_depth: int = 3,
    s4: S4Client | None = None,
) -> str:
    """
    Full multi-level BOM cost explosion across multiple plants.

    For each component at every level:
      - Fetches standard/moving-average price per plant
      - Fetches procurement type (Make=E / Buy=F / Both=X)
      - Fetches purchase info records to detect single vs multi-supplier
      - Computes cross-plant cost variance at each level
      - Classifies each component: Make / Buy / Strategic Buy
      - Identifies which components drive the cost difference between plants
      - Recommends the cheaper sourcing strategy per component

    Returns a structured JSON with:
      - perPlant: {plant: {totalCost, currency, breakdown}}
      - variance: {component: {plant1Cost, plant2Cost, diff, pct, driver}}
      - recommendations: [{component, action, reason, saving}]
      - costDrivers: [top components by absolute cost difference]
    """
    try:
        client = s4 or S4Client()

        # ── Step 1: Discover plants ──────────────────────────────────────────
        if not plants:
            raw = await list_material_plants(material_number, s4=client)
            pd = json.loads(raw)
            plants = [p["plant"] for p in pd.get("plants", [])]
        if not plants:
            return json.dumps({"error": f"No plants found for {material_number}", "materialNumber": material_number})

        # ── Step 2: Recursive BOM explosion ─────────────────────────────────
        async def explode(mat: str, plant: str, depth: int, visited: set) -> list:
            """Return flat list of components {material, description, qty, unit, level, plant}"""
            key = (mat, plant)
            if depth > max_depth or key in visited:
                return []
            visited.add(key)

            raw = await get_bom_explosion(mat, plant, "1", "single", s4=client)
            data = json.loads(raw)
            components = data.get("components", [])
            result = []
            for c in components:
                comp = c.get("componentMaterial", "")
                if not comp:
                    continue
                node = {
                    "material": comp,
                    "description": c.get("componentDescription", ""),
                    "quantity": float(c.get("quantity", 1)),
                    "unit": c.get("unit", ""),
                    "level": depth,
                    "plant": plant,
                    "parent": mat,
                    "bomNumber": c.get("bomNumber", ""),
                }
                result.append(node)
                # Recurse — check if this component itself has a BOM
                children = await explode(comp, plant, depth + 1, visited.copy())
                for child in children:
                    child["quantity"] = child["quantity"] * node["quantity"]  # roll up qty
                # A node's standalone price already reflects the rolled-up value of its
                # own children (SAP cumulative/standard costing). Mark whether this node
                # has children so the cost rollup step never sums a parent's price AND
                # its children's prices together — that would double- (or triple-)count
                # the same value once per level of the branch.
                node["hasChildren"] = bool(children)
                result.extend(children)
            return result

        # ── Step 3: Fetch valuation + procurement for every unique component ─
        async def enrich(mat: str, plant: str) -> dict:
            """Get price, procurement type, supplier count for one material/plant."""
            val_raw = await get_material_valuation(mat, plant, s4=client)
            val = json.loads(val_raw)
            proc_raw = await get_material_procurement_type(mat, plant, s4=client)
            proc = json.loads(proc_raw)
            pir_raw = await get_purchase_info_records(mat, plant, s4=client)
            pir = json.loads(pir_raw)

            price = float(val.get("effectivePrice") or val.get("standardPrice") or val.get("movingAveragePrice") or 0)
            currency = val.get("currency", "")
            ptype = proc.get("procurementType", "")
            vendor_count = pir.get("vendorCount", 0)
            vendors = [v.get("vendorCode", "") for v in pir.get("infoRecords", [])]

            return {
                "plant": plant,
                "price": price,
                "currency": currency,
                "priceControl": val.get("priceControlLabel", ""),
                "procurementType": ptype,
                "procurementTypeName": proc.get("procurementTypeName", ""),
                "vendorCount": vendor_count,
                "vendors": vendors,
                "hasMultipleSuppliers": vendor_count > 1,
                "totalStock": float(val.get("totalStock") or 0),
            }

        # ── Step 4: Run explosion for all plants in parallel-ish ────────────
        all_bom_nodes: dict[str, list] = {}  # plant → flat component list
        for plant in plants:
            visited: set = set()
            nodes = await explode(material_number, plant, 1, visited)
            all_bom_nodes[plant] = nodes

        # Collect all unique materials across all plants
        all_mats: set[str] = set()
        for nodes in all_bom_nodes.values():
            for n in nodes:
                all_mats.add(n["material"])
        all_mats.add(material_number)  # include the top level itself

        # ── Step 5: Enrich all materials across all plants ───────────────────
        enrichment: dict[str, dict[str, dict]] = {}  # mat → plant → data
        for mat in all_mats:
            enrichment[mat] = {}
            for plant in plants:
                try:
                    enrichment[mat][plant] = await enrich(mat, plant)
                except Exception as e:
                    enrichment[mat][plant] = {"plant": plant, "price": 0, "currency": "", "error": str(e)}

        # ── Step 6: Compute per-plant total cost (rolled-up) ─────────────────
        # IMPORTANT — avoid double-counting: a parent/assembly's own standalone
        # price already represents the rolled-up value of everything below it
        # (SAP standard/cumulative costing). So the true bottom-up cost of the
        # whole tree is the sum of LEAF nodes only (components with no BOM of
        # their own). Every non-leaf node is still shown in the breakdown for
        # transparency, but it's excluded from totalCost via includedInTotal.
        per_plant: dict[str, dict] = {}
        for plant in plants:
            nodes = all_bom_nodes.get(plant, [])
            breakdown = []
            total = 0.0
            currency = ""
            for n in nodes:
                mat = n["material"]
                qty = n["quantity"]
                edata = enrichment.get(mat, {}).get(plant, {})
                price = float(edata.get("price") or 0)
                line_cost = price * qty
                is_leaf = not n.get("hasChildren", False)
                if is_leaf:
                    total += line_cost
                if not currency and edata.get("currency"):
                    currency = edata["currency"]
                breakdown.append({
                    "material": mat,
                    "description": n.get("description", ""),
                    "level": n["level"],
                    "parent": n.get("parent", ""),
                    "quantity": qty,
                    "unit": n.get("unit", ""),
                    "unitPrice": price,
                    "lineCost": round(line_cost, 2),
                    "currency": edata.get("currency", currency),
                    "procurementType": edata.get("procurementType", ""),
                    "procurementTypeName": edata.get("procurementTypeName", ""),
                    "vendorCount": edata.get("vendorCount", 0),
                    "vendors": edata.get("vendors", []),
                    "hasMultipleSuppliers": edata.get("hasMultipleSuppliers", False),
                    "priceControl": edata.get("priceControl", ""),
                    "isLeafComponent": is_leaf,
                    "includedInTotal": is_leaf,
                })
            per_plant[plant] = {
                "totalCost": round(total, 2),
                "currency": currency,
                "componentCount": len(nodes),
                "leafComponentCount": sum(1 for n in nodes if not n.get("hasChildren", False)),
                "costingNote": (
                    "totalCost sums LEAF-level components only (quantity-rolled-up) to avoid "
                    "double-counting — an assembly's own standalone price already includes the "
                    "value of its sub-components, so it is excluded from the sum. Use each line's "
                    "'includedInTotal' flag; do not add the price of a parent AND its children."
                ),
                "breakdown": breakdown,
            }

        # ── Step 7: Cross-plant variance per component ────────────────────────
        if len(plants) >= 2:
            p1, p2 = plants[0], plants[1]
            variance_rows = []
            for mat in sorted(all_mats):
                e1 = enrichment.get(mat, {}).get(p1, {})
                e2 = enrichment.get(mat, {}).get(p2, {})
                price1 = float(e1.get("price") or 0)
                price2 = float(e2.get("price") or 0)
                if price1 == 0 and price2 == 0:
                    continue
                diff = price2 - price1
                pct = round(abs(diff) / max(price1, price2, 0.01) * 100, 2)
                cheaper_plant = p1 if price1 <= price2 else p2
                variance_rows.append({
                    "material": mat,
                    "description": enrichment.get(mat, {}).get(p1, {}).get("description", ""),
                    f"price_{p1}": price1,
                    f"price_{p2}": price2,
                    f"currency_{p1}": e1.get("currency", ""),
                    f"currency_{p2}": e2.get("currency", ""),
                    "absoluteDiff": round(abs(diff), 2),
                    "variancePct": pct,
                    "cheaperPlant": cheaper_plant,
                    "procType_p1": e1.get("procurementType", ""),
                    "procType_p2": e2.get("procurementType", ""),
                    "vendors_p1": e1.get("vendors", []),
                    "vendors_p2": e2.get("vendors", []),
                    "isSignificant": pct >= 5,
                    "isMajorDriver": pct >= 20,
                })
            # Sort by absolute diff descending
            variance_rows.sort(key=lambda x: x["absoluteDiff"], reverse=True)
        else:
            variance_rows = []

        # ── Step 8: Generate recommendations ─────────────────────────────────
        recommendations = []
        for row in variance_rows:
            mat = row["material"]
            pct = row["variancePct"]
            cheaper = row["cheaperPlant"]
            expensive = p2 if cheaper == p1 else p1
            pt1 = row.get("procType_p1", "")
            pt2 = row.get("procType_p2", "")
            v1 = row.get("vendors_p1", [])
            v2 = row.get("vendors_p2", [])

            if pct < 3:
                continue  # immaterial difference

            # Determine classification and recommendation
            if cheaper == p1:
                cheaper_proc = pt1
                cheaper_vendors = v1
                expensive_proc = pt2
            else:
                cheaper_proc = pt2
                cheaper_vendors = v2
                expensive_proc = pt1

            if expensive_proc == "E" and cheaper_proc == "F":
                action = "SWITCH_TO_BUY"
                reason = (
                    f"{expensive} currently makes {mat} in-house (procurement type E). "
                    f"{cheaper} buys it externally at {float(pct or 0):.1f}% lower cost. "
                    f"Recommend switching {expensive} to external procurement."
                )
            elif expensive_proc == "F" and cheaper_proc == "E":
                action = "SWITCH_TO_MAKE"
                reason = (
                    f"{expensive} buys {mat} externally. "
                    f"{cheaper} makes it in-house at {float(pct or 0):.1f}% lower cost. "
                    f"Evaluate in-house production at {expensive}."
                )
            elif not cheaper_vendors and pct >= 20:
                action = "STRATEGIC_BUY"
                reason = (
                    f"High variance ({float(pct or 0):.1f}%) with single-supplier dependency. "
                    f"Negotiate long-term contract or qualify alternative suppliers."
                )
            elif pct >= 20:
                action = "RENEGOTIATE"
                reason = (
                    f"Significant price gap ({float(pct or 0):.1f}%) between plants. "
                    f"Leverage {cheaper}'s pricing to renegotiate at {expensive}."
                )
            elif pct >= 5:
                action = "CONSOLIDATE"
                reason = (
                    f"Moderate variance ({float(pct or 0):.1f}%). "
                    f"Consider consolidating procurement to {cheaper} for volume discount."
                )
            else:
                continue

            saving_per_unit = row["absoluteDiff"]
            recommendations.append({
                "material": mat,
                "action": action,
                "cheaperPlant": cheaper,
                "expensivePlant": expensive,
                "variancePct": pct,
                "savingPerUnit": saving_per_unit,
                "reason": reason,
            })

        # ── Step 9: Cost drivers (top 5 by absolute diff) ────────────────────
        cost_drivers = variance_rows[:5]

        return json.dumps({
            "materialNumber": material_number,
            "plants": plants,
            "maxDepth": max_depth,
            "perPlant": per_plant,
            "crossPlantVariance": variance_rows,
            "costDrivers": cost_drivers,
            "recommendations": recommendations,
            "summary": {
                "cheapestPlant": min(per_plant.items(), key=lambda x: x[1]["totalCost"])[0] if per_plant else None,
                "totalVariancePct": round(
                    abs(per_plant.get(plants[0], {}).get("totalCost", 0) -
                        per_plant.get(plants[1], {}).get("totalCost", 0)) /
                    max(per_plant.get(plants[0], {}).get("totalCost", 0),
                        per_plant.get(plants[1], {}).get("totalCost", 0), 1) * 100, 2
                ) if len(plants) >= 2 else 0,
                "significantDrivers": len([r for r in variance_rows if r.get("isMajorDriver")]),
                "recommendationCount": len(recommendations),
            },
            "status": "success",
        })

    except Exception as e:
        logger.exception("[deep_bom_cost_analysis] error")
        return json.dumps({"error": str(e), "materialNumber": material_number})


async def trigger_procurement_review_workflow(
    analysis_id: str,
    priority: str = "Medium",
    comments: str | None = None,
) -> str:
    """Trigger procurement review workflow via the CAP WorkflowTriggerService."""
    try:
        result = await _call_cap(
            "/api/triggerProcurementReview",
            method="POST",
            body={"analysisId": analysis_id, "priority": priority, "comments": comments or ""},
        )
        logger.info(f"[M2.achieved: procurement workflow triggered for {analysis_id}]")
        return json.dumps(result)
    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        logger.error(f"[M2.missed: procurement workflow trigger failed for {analysis_id}, reason={error_msg}]")
        return json.dumps({"error": error_msg, "analysisId": analysis_id})
    except Exception as e:
        logger.error(f"[M2.missed: procurement workflow trigger failed for {analysis_id}, reason={e}]")
        return json.dumps({"error": str(e), "analysisId": analysis_id})


async def trigger_costing_review_workflow(
    analysis_id: str,
    cost_center: str | None = None,
    comments: str | None = None,
) -> str:
    """Trigger costing review workflow via the CAP WorkflowTriggerService."""
    try:
        result = await _call_cap(
            "/api/triggerCostingReview",
            method="POST",
            body={"analysisId": analysis_id, "costCenter": cost_center or "", "comments": comments or ""},
        )
        logger.info(f"[M2.achieved: costing workflow triggered for {analysis_id}]")
        return json.dumps(result)
    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        logger.error(f"[M2.missed: costing workflow trigger failed for {analysis_id}, reason={error_msg}]")
        return json.dumps({"error": error_msg, "analysisId": analysis_id})
    except Exception as e:
        logger.error(f"[M2.missed: costing workflow trigger failed for {analysis_id}, reason={e}]")
        return json.dumps({"error": str(e), "analysisId": analysis_id})


async def get_cheapest_material_valuation(
    material_number: str,
    s4: S4Client | None = None,
) -> str:
    """
    Find the cheapest plant for a material automatically.

    Internally:
      1. Calls list_material_plants to discover ALL plants
      2. Calls get_material_valuation for EVERY plant
      3. Converts all prices to INR using live SAP FX rates
      4. Returns ranked list with cheapestPlant + cheapestPriceINR first

    Use this for HYPOTHETICAL PLANNING instead of calling
    list_material_plants + get_material_valuation separately.
    The result's `cheapestPriceINR` is ready to use directly in the total
    assembly cost formula — no further FX conversion needed.
    """
    try:
        # Step 1: discover all plants
        plants_raw = await list_material_plants(material_number, s4=s4)
        plants_data = json.loads(plants_raw)
        plant_codes = [p["plant"] for p in plants_data.get("plants", [])]

        if not plant_codes:
            return json.dumps({
                "materialNumber": material_number,
                "status": "no_plants_found",
                "cheapestPlant": None,
                "allPlants": [],
                "note": f"No plants found for {material_number} in SAP CIN_100",
            })

        # Step 2: fetch live FX rates (EUR→INR, USD→INR)
        fx_rates: dict[str, float] = {"INR": 1.0}
        for from_ccy in ("EUR", "USD"):
            try:
                fx_raw = await get_tcurr_fx_rate(from_ccy, "INR", s4=s4)
                fx_d = json.loads(fx_raw)
                if fx_d.get("rate") and float(fx_d["rate"]) > 0:
                    fx_rates[from_ccy] = float(fx_d["rate"])
            except Exception:
                pass
        # Fallbacks if SAP FX unavailable
        fx_rates.setdefault("EUR", 109.6)
        fx_rates.setdefault("USD", 95.69)

        # Step 3: get valuation at EVERY plant
        plant_valuations: list[dict] = []
        for plant in plant_codes:
            try:
                val_raw = await get_material_valuation(material_number, plant, s4=s4)
                val_d = json.loads(val_raw)
                effective = float(val_d.get("effectivePrice") or 0)
                if effective <= 0 or val_d.get("error"):
                    continue
                currency = (val_d.get("currency") or "EUR").upper()
                if currency not in fx_rates:
                    try:
                        cr = await get_tcurr_fx_rate(currency, "INR", s4=s4)
                        cd = json.loads(cr)
                        if cd.get("rate") and float(cd["rate"]) > 0:
                            fx_rates[currency] = float(cd["rate"])
                    except Exception:
                        fx_rates[currency] = 1.0
                price_inr = round(effective * fx_rates.get(currency, 1.0), 4)
                plant_valuations.append({
                    "plant": plant,
                    "effectivePrice": effective,
                    "currency": currency,
                    "priceINR": price_inr,
                    "priceControl": val_d.get("priceControl", ""),
                    "priceControlLabel": val_d.get("priceControlLabel", ""),
                    "standardPrice": val_d.get("standardPrice", 0),
                    "movingAveragePrice": val_d.get("movingAveragePrice", 0),
                    "fxRateUsed": fx_rates.get(currency, 1.0),
                })
            except Exception as ve:
                logger.warning("Valuation fetch failed for %s/%s: %s", material_number, plant, ve)

        if not plant_valuations:
            return json.dumps({
                "materialNumber": material_number,
                "status": "no_valuation_data",
                "cheapestPlant": None,
                "allPlants": plant_codes,
                "note": f"No price data found for {material_number} at any of {plant_codes}. "
                        f"Material may not have valuation records.",
            })

        # Step 4: sort by INR price — cheapest first
        plant_valuations.sort(key=lambda x: x["priceINR"])
        cheapest = plant_valuations[0]

        return json.dumps({
            "materialNumber": material_number,
            "status": "success",
            "cheapestPlant": cheapest["plant"],
            "cheapestEffectivePrice": cheapest["effectivePrice"],
            "cheapestCurrency": cheapest["currency"],
            "cheapestPriceINR": cheapest["priceINR"],
            "fxRatesUsed": {k: v for k, v in fx_rates.items() if k != "INR"},
            "rankedPlants": plant_valuations,
            "note": (
                f"Cheapest plant for {material_number} is {cheapest['plant']} "
                f"at {cheapest['effectivePrice']} {cheapest['currency']} = "
                f"₹{cheapest['priceINR']:,.4f} per unit "
                f"(FX: 1 {cheapest['currency']} = ₹{fx_rates.get(cheapest['currency'], 1.0):.4f}). "
                f"Use cheapestPriceINR directly for assembly cost — no further FX conversion needed."
            ),
        })
    except Exception as e:
        return json.dumps({"error": str(e), "materialNumber": material_number})


# ---------------------------------------------------------------------------
# Deterministic calculator (exposes calculator.py as a LangChain tool)
# ---------------------------------------------------------------------------

def run_cost_calculator(
    components: list,
    extra_charges: list = None,
    budget: float = 0.0,
    budget_currency: str = "USD",
) -> str:
    """
    Deterministic assembly cost + budget calculation.
    ALL arithmetic done in Python — never in the LLM's head.

    Accepts:
      components:    [{name, qty, price, currency, plant}, ...]
      extra_charges: [{name, amount, currency}, ...]
      budget:        total available budget (0 = skip budget analysis)
      budget_currency: USD | EUR | INR

    Returns JSON with:
      total_cost_per_unit_inr, units_affordable, total_spent_inr, remaining_inr,
      budget_inr, per-component breakdown.
    """
    try:
        from calculator import calculate_assembly_cost, format_inr
        if extra_charges is None:
            extra_charges = []
        result = calculate_assembly_cost(
            components=components,
            extra_charges=extra_charges,
            budget=budget,
            budget_currency=budget_currency,
        )
        # Format key monetary values for readability
        total = result.get("total_cost_per_unit_inr", 0)
        out = {
            "status": "success",
            "total_cost_per_unit_inr": total,
            "components": result.get("components", []),
            "extra_charges": result.get("extra_charges", []),
        }
        if budget > 0:
            budget_inr = result.get("budget_inr", 0)
            units = result.get("units_affordable", 0)
            spent = result.get("total_spent_inr", 0)
            remaining = result.get("remaining_inr", 0)
            out.update({
                "budget": budget,
                "budget_currency": budget_currency,
                "budget_inr": budget_inr,
                "units_affordable": units,
                "total_spent_inr": spent,
                "remaining_inr": remaining,
                "note": (
                    f"With budget {budget_currency} {budget:,.0f} = ₹{budget_inr:,.2f}: "
                    f"assembly costs ₹{total:,.2f}/unit → "
                    f"can build {units} assemblies, "
                    f"spent ₹{spent:,.2f}, remaining ₹{remaining:,.2f}."
                ),
            })
        return json.dumps(out)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def get_bom_tools(s4: S4Client | None = None) -> list[StructuredTool]:
    """Return all 12 BOM cost optimization tools as LangChain StructuredTool instances."""
    _client = s4 or S4Client()

    async def _get_bom_headers(material_number, plant=None, bom_usage=None, bom_category="M"):
        return await get_bom_headers(material_number, plant, bom_usage, bom_category, s4=_client)

    async def _get_material_by_bom_number(bom_number, plant=None):
        return await get_material_by_bom_number(bom_number, plant, s4=_client)

    async def _get_bom_explosion(material_number, plant, bom_usage="1", explosion_type="multi"):
        return await get_bom_explosion(material_number, plant, bom_usage, explosion_type, s4=_client)

    async def _list_material_plants(material_number):
        return await list_material_plants(material_number, s4=_client)

    async def _get_material_count(material_type=None):
        return await get_material_count(material_type, s4=_client)

    async def _get_tcurr_fx_rate(from_currency, to_currency, rate_type="M", date=None):
        return await get_tcurr_fx_rate(from_currency, to_currency, rate_type, date, s4=_client)

    async def _get_bom_cross_plant(vehicle_material, plant1, plant2, usage=None, alternative=None):
        return await get_bom_cross_plant(vehicle_material, plant1, plant2, usage, alternative, s4=_client)

    async def _get_material_procurement_type(material_number, plant):
        return await get_material_procurement_type(material_number, plant, s4=_client)

    async def _get_purchase_info_records(material_number, plant, purchasing_org=None):
        return await get_purchase_info_records(material_number, plant, purchasing_org, s4=_client)

    async def _get_purchase_order_history(material_number, plant, date_from=None, date_to=None):
        return await get_purchase_order_history(material_number, plant, date_from, date_to, s4=_client)

    async def _get_material_valuation(material_number, plant, valuation_area=None):
        return await get_material_valuation(material_number, plant, valuation_area, s4=_client)

    async def _get_cheapest_material_valuation(material_number):
        return await get_cheapest_material_valuation(material_number, s4=_client)

    def _run_cost_calculator(components, extra_charges=None, budget=0.0, budget_currency="USD"):
        return run_cost_calculator(components, extra_charges or [], budget, budget_currency)

    async def _get_bom_items_raw(bom_number, plant, bom_category="M", bom_variant="1"):
        return await get_bom_items_raw(bom_number, plant, bom_category, bom_variant, s4=_client)

    async def _deep_bom_cost_analysis(material_number, plants=None, max_depth=3):
        return await deep_bom_cost_analysis(material_number, plants, max_depth, s4=_client)

    async def _explain_bom_cost_variance(material_number, plant1, plant2, max_depth=6, variance_threshold_pct=1.0):
        return await explain_bom_cost_variance(material_number, plant1, plant2, max_depth, variance_threshold_pct, s4=_client)

    return [
        StructuredTool(
            name="get_material_by_bom_number",
            description=(
                "REVERSE BOM lookup: given a BOM NUMBER, find which MATERIAL it belongs to. "
                "Use this when user asks 'which material has BOM 00000052?', "
                "'material against BOM 00000023', 'what material is BOM number X for?', "
                "'elaborate BOM number 00000053'. "
                "This is DIFFERENT from get_bom_headers which goes Material → BOM. "
                "This tool goes BOM Number → Material (the reverse direction). "
                "ALWAYS use this tool when the user gives you a BOM NUMBER and asks for the MATERIAL."
            ),
            args_schema=GetMaterialByBomNumberInput,
            coroutine=_get_material_by_bom_number,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="get_bom_headers",
            description=(
                "Fetch BOM numbers (Bill of Material numbers) for a material from SAP CIN_100. "
                "Call this when user asks 'what is the BOM number', 'BOM numbers for material X', "
                "'which BOM is assigned to this material', 'BOM header for plant X'. "
                "Returns BOM number, category (M=Material), variant, plant, and usage type. "
                "If plant is omitted, returns BOMs across ALL plants where the material has a BOM. "
                "This is the PRIMARY tool for BOM number queries."
            ),
            args_schema=GetBomHeadersInput,
            coroutine=_get_bom_headers,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="get_bom_explosion",
            description=(
                "Explode a BOM (multi-level by default) to get all components/sub-components. "
                "Use when user asks 'explode BOM', 'what components does X contain', "
                "'multi-level BOM explosion', 'BOM structure', 'CS12 equivalent'. "
                "Returns all components with quantity, unit, and scrap percentage."
            ),
            args_schema=GetBomExplosionInput,
            coroutine=_get_bom_explosion,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="list_material_plants",
            description=(
                "ALWAYS call this FIRST when a user asks about a material without specifying plants. "
                "Discovers ALL plants where the material is maintained in SAP CIN_100. "
                "Returns real plant codes (numeric like 1010, 1710, 1720) — use these for all follow-up calls. "
                "NEVER guess plant codes. NEVER use MANE or GURG — those are not valid SAP plant codes in this system."
            ),
            args_schema=ListMaterialPlantsInput,
            coroutine=_list_material_plants,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="get_material_count",
            description=(
                "Get the total count of materials in SAP S/4HANA system CIN_100. "
                "Use when user asks 'how many materials are there', 'total material count', etc. "
                "Optionally filter by material type (ROH=Raw, HALB=Semi-finished, FERT=Finished)."
            ),
            args_schema=GetMaterialCountInput,
            coroutine=_get_material_count,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="get_tcurr_fx_rate",
            description=(
                "Fetch live FX exchange rate from SAP TCURR table in CIN_100 — the AUTHORITATIVE source. "
                "Always call this BEFORE doing any currency conversion. "
                "Automatically tries direct rate, then inverse, then indirect (via USD), then open.er-api.com fallback. "
                "Use this instead of hardcoded rates. Example: get_tcurr_fx_rate('EUR', 'INR') for EUR→INR."
            ),
            args_schema=GetTcurrFxRateInput,
            coroutine=_get_tcurr_fx_rate,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="get_bom_cross_plant",
            description=(
                "Retrieve and compare BOM component costs for a vehicle material across two manufacturing plants. "
                "Returns cost per component for each plant, absolute and percentage variance, and total impact INR."
            ),
            args_schema=GetBomCrossPlantInput,
            coroutine=_get_bom_cross_plant,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="get_material_procurement_type",
            description=(
                "Retrieve the procurement type for a material at a plant from S/4HANA material master. "
                "E=In-house production, F=External procurement, X=Both. Used for Make/Buy classification."
            ),
            args_schema=GetMaterialProcurementTypeInput,
            coroutine=_get_material_procurement_type,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="get_purchase_info_records",
            description=(
                "Retrieve purchase info records for a material, showing vendor prices and supplier count. "
                "Used to detect single-supplier dependency (triggers Strategic Buy classification)."
            ),
            args_schema=GetPurchaseInfoRecordsInput,
            coroutine=_get_purchase_info_records,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="get_purchase_order_history",
            description=(
                "Retrieve purchase order history for a material at a plant. "
                "Shows historical quantities, prices, and posting dates for trend analysis."
            ),
            args_schema=GetPurchaseOrderHistoryInput,
            coroutine=_get_purchase_order_history,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="get_material_valuation",
            description=(
                "Retrieve material valuation data (moving average price, standard price) for a material at a plant. "
                "Used to validate BOM component costs against current valuation."
            ),
            args_schema=GetMaterialValuationInput,
            coroutine=_get_material_valuation,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="get_cheapest_material_valuation",
            description=(
                "Find the CHEAPEST PLANT for a material automatically — use this for HYPOTHETICAL PLANNING "
                "and any scenario where you need to compare prices across ALL plants without specifying one. "
                "Internally: discovers all plants, fetches price at each, converts to INR, returns sorted list. "
                "Returns: cheapestPlant, cheapestPriceINR (ready-to-use, no FX needed), cheapestEffectivePrice, "
                "cheapestCurrency, rankedPlants (all plants sorted cheapest→most expensive). "
                "ALWAYS use this instead of calling list_material_plants + get_material_valuation separately "
                "when doing HYPOTHETICAL PLANNING or CHEAPEST SOURCING analysis."
            ),
            args_schema=GetCheapestMaterialValuationInput,
            coroutine=_get_cheapest_material_valuation,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="run_cost_calculator",
            description=(
                "DETERMINISTIC Python calculator — use this for ALL arithmetic in HYPOTHETICAL PLANNING "
                "and BUDGET ANALYSIS. NEVER do floor division or large-number arithmetic yourself — "
                "always call this tool. "
                "Pass: components (list of {name, qty, price, currency, plant}), "
                "extra_charges (list of {name, amount, currency}), "
                "budget (numeric value, 0 = skip budget), budget_currency (USD/EUR/INR). "
                "Returns: total_cost_per_unit_inr (assembly cost), units_affordable (floor division), "
                "remaining_inr (exact remaining budget), total_spent_inr, budget_inr. "
                "CRITICAL RULES: "
                "(1) budget = RAW number in original currency. e.g. 15000000 for '15M EUR'. "
                "    NEVER pass a pre-converted INR value. WRONG: budget=1767000000000, budget_currency='INR'. "
                "    CORRECT: budget=15000000, budget_currency='EUR'. The tool converts internally. "
                "(2) Do NOT call get_tcurr_fx_rate and multiply budget × rate before calling this tool. "
                "    That causes a 1000× error. Pass the original currency directly. "
                "(3) Use cheapestPriceINR from get_cheapest_material_valuation as component price. "
                "    Set currency='INR' when price is already in INR (no double-conversion). "
                "(4) Read ALL results from this tool's output — units_affordable, remaining_inr, "
                "    budget_inr — never compute them yourself. remaining_inr is NEVER ₹0 unless the "
                "    budget was exactly divisible."
            ),
            args_schema=RunCostCalculatorInput,
            func=_run_cost_calculator,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="trigger_procurement_review_workflow",
            description=(
                "Trigger a procurement review workflow for a saved BOM analysis. "
                "Sends the analysis to the procurement team via the n8n workflow. "
                "Requires a valid analysis_id from the CAP service (BomAnalysis.ID)."
            ),
            args_schema=TriggerProcurementReviewInput,
            coroutine=trigger_procurement_review_workflow,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="explain_bom_cost_variance",
            description=(
                "RECURSIVE cost variance explainer between two plants. "
                "Use when user asks: 'why is cost different', 'explain cost difference', "
                "'drill down to find what drives the price gap', 'which component is responsible', "
                "'why is plant X more expensive', 'sub-component cost explanation', "
                "'level by level cost breakdown', 'root cause of price variance'. "
                "Walks the entire BOM tree from top to deepest level, at each node explains: "
                "same/different price, currency impact, procurement type (Make vs Buy), "
                "supplier names and pricing, quantity differences. "
                "Returns tree structure + flat explanation + top cost drivers + recommendations. "
                "ALWAYS use this tool when user asks WHY costs differ between plants."
            ),
            args_schema=ExplainBomCostVarianceInput,
            coroutine=_explain_bom_cost_variance,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="deep_bom_cost_analysis",
            description=(
                "DEEP multi-level BOM cost analysis across plants. "
                "Use this when user asks for: deep dive, root cause of cost difference, "
                "'why is cost different between plants', 'drill down BOM hierarchy', "
                "'sub-component cost analysis', 'make vs buy recommendation', "
                "'which component is responsible for cost difference', "
                "'cost optimization across plants', 'lowest level BOM cost'. "
                "This tool: (1) explodes BOM to all levels, (2) fetches price/procurement/suppliers "
                "for every component at every plant, (3) computes cross-plant variance per component, "
                "(4) identifies top cost drivers, (5) recommends Make/Buy/Strategic Buy/Consolidate "
                "actions with estimated savings per unit. Returns full structured analysis. "
                "IMPORTANT: perPlant.totalCost already sums LEAF-level components only, so it is "
                "NOT double-counted — do not re-sum breakdown lines yourself, and never add a "
                "parent assembly's unitPrice on top of its own children's unitPrice (check each "
                "line's includedInTotal flag). For 'which plant should I buy each component from' "
                "style questions, use crossPlantVariance[i].cheaperPlant PER COMPONENT — do not "
                "collapse the answer to summary.cheapestPlant, which is only the cheapest plant if "
                "you bought EVERYTHING from one single plant and is rarely the optimal mix."
            ),
            args_schema=DeepBomCostAnalysisInput,
            coroutine=_deep_bom_cost_analysis,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="get_bom_items_raw",
            description=(
                "RAW BOM items diagnostic tool. Use this when get_bom_explosion returns no components. "
                "Tries every possible API strategy ($expand, navigation, $filter) on both BOM services. "
                "Call this with the BOM number (e.g. '00000381') and plant to diagnose why components are missing. "
                "Returns actual components if found, or a detailed diagnosis if not."
            ),
            args_schema=GetBomItemsRawInput,
            coroutine=_get_bom_items_raw,
            handle_tool_error=True,
        ),
        StructuredTool(
            name="trigger_costing_review_workflow",
            description=(
                "Trigger a costing review workflow for a saved BOM analysis. "
                "Sends the analysis to the costing team via the n8n workflow. "
                "Requires a valid analysis_id from the CAP service (BomAnalysis.ID)."
            ),
            args_schema=TriggerCostingReviewInput,
            coroutine=trigger_costing_review_workflow,
            handle_tool_error=True,
        ),
    ]

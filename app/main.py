"""
A2A server entry point — dual-mode (Joule Studio + Cloud Foundry).
Also exposes:
  POST /api/analyse  — structured BOM + valuation + procurement data from S/4HANA
  POST /api/chat     — conversational agent with context/thread support
"""
from __future__ import annotations

import json as _json
import logging
import os
import uuid

_JOULE = os.environ.get("JOULE_RUNTIME", "0") == "1"

if _JOULE:
    try:
        from sap_cloud_sdk.aicore import set_aicore_config       # type: ignore
        from sap_cloud_sdk.core.telemetry import auto_instrument  # type: ignore
        set_aicore_config()
        auto_instrument()
    except ImportError:
        pass

import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from agent_executor import AgentExecutor
from agent import get_live_fx_rates
from query_understander import understand_query
from entity_extractor import extract_entities
from tools import (
    classify_make_buy,
    get_bom_cross_plant,
    get_material_procurement_type,
    get_material_valuation,
    get_purchase_info_records,
    get_purchase_order_history,
    record_feedback,
    get_feedback_summary,
    get_training_examples,
    get_feedback_stats,
    _detect_question_type,
)

if _JOULE:
    try:
        from mcp_tools import set_user_token  # type: ignore
    except ImportError:
        def set_user_token(tok): pass
else:
    def set_user_token(tok): pass

from s4hana_client import set_request_auth, clear_request_auth

# ── In-memory token → credentials store ──────────────────────────────────────
# When a user logs in, we store their SAP credentials keyed by token UUID.
# These are forwarded on every /api call so SAP sees the real user identity.
_TOKEN_STORE: dict[str, dict] = {}

def store_token(token: str, username: str, password: str) -> None:
    _TOKEN_STORE[token] = {"username": username, "password": password}
    # Keep store bounded — remove oldest entries beyond 500
    if len(_TOKEN_STORE) > 500:
        oldest = next(iter(_TOKEN_STORE))
        del _TOKEN_STORE[oldest]

def resolve_token(token: str) -> dict | None:
    return _TOKEN_STORE.get(token)

try:
    from opentelemetry.instrumentation.starlette import StarletteInstrumentor
    _OTEL = True
except ImportError:
    _OTEL = False

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))

# ── Singleton executor (reuse S4Client across requests) ──────────────────────
_executor: AgentExecutor | None = None


def get_executor() -> AgentExecutor:
    global _executor
    if _executor is None:
        _executor = AgentExecutor()
    return _executor


# ── Middleware ────────────────────────────────────────────────────────────────
_DEFAULT_SAP_USER = os.environ.get("DEFAULT_SAP_USERNAME", "")
_DEFAULT_SAP_PASS = os.environ.get("DEFAULT_SAP_PASSWORD", "")


class JWTContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        auth = request.headers.get("authorization", "")
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        set_user_token(tok or None)
        if tok:
            creds = resolve_token(tok)
            if creds:
                set_request_auth(creds["username"], creds["password"])
        elif _DEFAULT_SAP_USER and _DEFAULT_SAP_PASS:
            # No user token — fall back to the service-account credentials so
            # MCP / API callers (who have no SAP login) still get live data.
            set_request_auth(_DEFAULT_SAP_USER, _DEFAULT_SAP_PASS)
        try:
            return await call_next(request)
        finally:
            set_user_token(None)
            clear_request_auth()


# ═══════════════════════════════════════════════════════════════════════════════
#  /api/analyse  — fetch real S/4HANA data for materials × plants
# ═══════════════════════════════════════════════════════════════════════════════
async def api_login(request: Request) -> JSONResponse:
    """
    POST /api/login
    Validates SAP CIN_100 credentials strictly — no fallback to valid=True.
    Returns token on success, 401 on bad credentials, 503 if CIN_100 unreachable.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    username = body.get("username", "").strip()
    password = body.get("password", "")
    language = body.get("language", "EN")

    if not username or not password:
        return JSONResponse({"error": "Username and password required"}, status_code=400)

    exc = get_executor()
    s4  = exc.agent.s4_client
    try:
        import httpx as _httpx, base64 as _b64
        dest = await s4.destination()
        creds_b64 = _b64.b64encode(f"{username}:{password}".encode()).decode()

        client_kwargs: dict = {"timeout": 15.0}
        extra_headers: dict = {}
        if dest.proxy_type.lower() == "onpremise":
            try:
                proxy_url, proxy_hdr = await s4._resolver.proxy_settings()
                client_kwargs["proxy"] = proxy_url
                extra_headers.update(proxy_hdr)
            except Exception as pe:
                logger.warning("[login] proxy setup failed: %s", pe)

        ping_url = dest.url.rstrip("/") + "/sap/opu/odata/sap/API_PRODUCT_SRV/"
        params = {}
        if dest.sap_client:
            params["sap-client"] = dest.sap_client

        async with _httpx.AsyncClient(**client_kwargs) as hc:
            r = await hc.get(
                ping_url,
                headers={
                    "Authorization": f"Basic {creds_b64}",
                    "x-csrf-token": "Fetch",
                    "Accept": "application/json",
                    **extra_headers,
                },
                params=params,
            )

        logger.info("[login] CIN_100 status=%d user=%s", r.status_code, username)

        if r.status_code in (200, 201):
            token = str(uuid.uuid4())
            store_token(token, username, password)
            logger.info("[login] SUCCESS user=%s token=%s", username, token[:8])
            return JSONResponse({
                "token": token,
                "username": username,
                "language": language,
                "system": "CIN_100",
            })
        elif r.status_code in (401, 403):
            logger.warning("[login] REJECTED invalid credentials user=%s", username)
            return JSONResponse(
                {"error": "Invalid CIN_100 credentials. Please check your username and password."},
                status_code=401,
            )
        else:
            # Any unexpected SAP status — STRICT REJECT, no bypass allowed
            logger.warning("[login] REJECTED unexpected SAP status=%d user=%s", r.status_code, username)
            return JSONResponse(
                {"error": "CIN_100 credentials could not be verified. Please check your username and password."},
                status_code=401,
            )

    except Exception as e:
        logger.warning("[login] CIN_100 connection error: %s", e)
        # Connection failure — return 503 so UI shows "system unavailable", NOT 200
        return JSONResponse(
            {"error": "Cannot connect to CIN_100 system. Please try again later."},
            status_code=503,
        )



async def api_validate_credentials(request: Request) -> JSONResponse:
    """
    POST /api/validate-credentials
    Body: { username, password, language }
    Validates SAP CIN_100 credentials via CSRF token handshake through BTP destination service.
    Returns: { valid: true } or HTTP 401
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        return JSONResponse({"error": "Username and password required"}, status_code=400)

    # Use the S4 client's destination to validate — ping the OData service root
    exc = get_executor()
    s4 = exc.agent.s4_client
    try:
        import httpx as _httpx
        dest = await s4.destination()
        import base64 as _b64
        creds_b64 = _b64.b64encode(f"{username}:{password}".encode()).decode()

        client_kwargs: dict = {"timeout": 15.0}
        extra_headers: dict = {}

        if dest.proxy_type.lower() == "onpremise":
            try:
                proxy_url, proxy_hdr = await s4._resolver.proxy_settings()
                client_kwargs["proxy"] = proxy_url
                extra_headers.update(proxy_hdr)
            except Exception as pe:
                logger.warning("[validate-credentials] proxy setup failed: %s", pe)

        ping_url = dest.url.rstrip("/") + "/sap/opu/odata/sap/API_PRODUCT_SRV/"
        params = {}
        if dest.sap_client:
            params["sap-client"] = dest.sap_client

        async with _httpx.AsyncClient(**client_kwargs) as hc:
            r = await hc.get(
                ping_url,
                headers={
                    "Authorization": f"Basic {creds_b64}",
                    "x-csrf-token": "Fetch",
                    "Accept": "application/json",
                    **extra_headers,
                },
                params=params,
            )

        logger.info("[validate-credentials] SAP status=%d user=%s", r.status_code, username)

        if r.status_code in (200, 201):
            return JSONResponse({"valid": True, "username": username})
        elif r.status_code in (401, 403):
            return JSONResponse({"valid": False, "error": "Invalid credentials"}, status_code=401)
        else:
            # Unexpected status — allow login (destination may have different auth)
            return JSONResponse({"valid": True, "username": username})

    except Exception as e:
        logger.warning("[validate-credentials] error: %s", e)
        # Cannot reach SAP — return 503, do NOT allow login bypass
        return JSONResponse({"valid": False, "error": "Cannot connect to CIN_100. Please try again."}, status_code=503)


async def api_analyse(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # ── Wildcard material search (searchOnly=true) ───────────────────────────
    if body.get("searchOnly"):
        pattern = body.get("search", "").strip().upper()
        exc = get_executor()
        s4  = exc.agent.s4_client
        base = pattern.rstrip("*").rstrip("?").strip()
        if not base:
            return JSONResponse({"materials": []})

        mats = []
        seen = set()

        # Strategy 1: Standard Product API
        try:
            result = await s4.get(
                "sap/opu/odata/sap/API_PRODUCT_SRV/A_Product",
                params={"$filter": f"startswith(Product,'{base}')", "$select": "Product", "$top": "20"}
            )
            for i in result.get("results", []):
                p = i.get("Product","")
                if p and p not in seen:
                    seen.add(p); mats.append(p)
            logger.info("Material search S1 '%s*' -> %s results", base, len(mats))
        except Exception as e:
            logger.warning("Material search S1 error: %s", e)

        # Strategy 2: BOM table — catches demo/training materials like F-10A
        if not mats:
            try:
                result = await s4.get(
                    "sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV_0002/A_BillOfMaterial",
                    params={"$filter": f"startswith(Material,'{base}')", "$select": "Material", "$top": "20"}
                )
                for i in result.get("results", []):
                    p = i.get("Material","")
                    if p and p not in seen:
                        seen.add(p); mats.append(p)
                logger.info("Material search S2 (BOM) '%s*' -> %s results", base, len(mats))
            except Exception as e:
                logger.warning("Material search S2 (BOM) error: %s", e)

        # Strategy 3: Valuation table — another fallback
        if not mats:
            try:
                result = await s4.get(
                    "sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductValuation",
                    params={"$filter": f"startswith(Product,'{base}')", "$select": "Product", "$top": "20"}
                )
                for i in result.get("results", []):
                    p = i.get("Product","")
                    if p and p not in seen:
                        seen.add(p); mats.append(p)
                logger.info("Material search S3 (Valuation) '%s*' -> %s results", base, len(mats))
            except Exception as e:
                logger.warning("Material search S3 error: %s", e)

        # Strategy 4: Exact match check
        if not mats and '*' not in pattern and len(base) >= 2:
            try:
                result = await s4.get(
                    "sap/opu/odata/sap/API_PRODUCT_SRV/A_Product",
                    params={"$filter": f"Product eq '{base}'", "$select": "Product", "$top": "1"}
                )
                for i in result.get("results", []):
                    p = i.get("Product","")
                    if p and p not in seen:
                        seen.add(p); mats.append(p)
            except Exception as e:
                logger.warning("Material search S4 error: %s", e)

        return JSONResponse({"materials": mats})

    materials: list[str] = body.get("materials", [])
    plants: list[str]    = body.get("plants", [])

    if not materials or len(plants) < 2:
        return JSONResponse({"error": "Need ≥1 material and ≥2 plants"}, status_code=400)

    exc = get_executor()
    s4  = exc.agent.s4_client
    out = {}

    for mat in materials:
        mat_result: dict = {"material": mat, "plants": {}, "found": False}

        # ── per-plant: procurement type + valuation ──────────────────────────
        any_found = False
        for plant in plants:
            pdata: dict = {}

            try:
                raw = await get_material_procurement_type(mat, plant, s4=s4)
                p = _json.loads(raw)
                if p.get("error") == "not_found":
                    pdata["invalid"] = True
                    pdata["invalidReason"] = p.get("message", f"Material '{mat}' not found in plant {plant}")
                elif not p.get("error"):
                    pdata["procurementType"]     = p.get("procurementType", "")
                    pdata["procurementTypeName"] = p.get("procurementTypeName", "")
                    pdata["mrpType"]             = p.get("mrpType", "")
                    any_found = True
                else:
                    pdata["procurementError"] = p.get("error", "")
            except Exception as e:
                pdata["procurementError"] = str(e)

            try:
                raw = await get_material_valuation(mat, plant, s4=s4)
                v = _json.loads(raw)
                if not v.get("error"):
                    # ── Valuation setup ──
                    pdata["priceControl"]        = v.get("priceControl", "")
                    pdata["priceControlLabel"]   = v.get("priceControlLabel", "")
                    pdata["valuationClass"]      = v.get("valuationClass", "")
                    pdata["valuationCategory"]   = v.get("valuationCategory", "")
                    pdata["valuationType"]       = v.get("valuationType", "")
                    pdata["priceUnit"]           = v.get("priceUnit", 1)
                    pdata["currency"]            = v.get("currency", "")
                    # ── Prices ──
                    pdata["standardPrice"]       = v.get("standardPrice", 0)
                    pdata["movingAvgPrice"]      = v.get("movingAveragePrice", 0)
                    pdata["effectivePrice"]      = v.get("effectivePrice", 0)
                    pdata["oldStandardPrice"]    = v.get("oldStandardPrice", 0)
                    pdata["lastPriceChangeDate"] = v.get("lastPriceChangeDate", "")
                    # ── Inventory ──
                    pdata["totalStock"]          = v.get("totalStock", 0)
                    pdata["totalStockValue"]     = v.get("totalStockValue", 0)
                    pdata["valuationNote"]       = v.get("note", "")
                    any_found = True
                else:
                    pdata["valuationError"] = v.get("error", "")
            except Exception as e:
                pdata["valuationError"] = str(e)

            mat_result["plants"][plant] = pdata

        mat_result["found"] = any_found

        if not any_found:
            mat_result["errorMessage"] = (
                f"Material '{mat}' not found in SAP system (destination CIN_100). "
                "Please verify the material number exists in the connected S/4HANA system."
            )
            out[mat] = mat_result
            continue

        # ── info records (not plant-specific) ────────────────────────────────
        try:
            raw = await get_purchase_info_records(mat, plants[0], s4=s4)
            pir = _json.loads(raw)
            mat_result["infoRecords"] = {
                "vendorCount":        pir.get("vendorCount", 0),
                "hasMultipleSuppliers": pir.get("hasMultipleSuppliers", False),
                "vendors":            pir.get("infoRecords", []),
                "error":              pir.get("error", ""),
            }
        except Exception as e:
            mat_result["infoRecords"] = {"error": str(e), "vendorCount": 0, "vendors": []}

        # ── PO history per plant ──────────────────────────────────────────────
        mat_result["poHistory"] = {}
        for plant in plants:
            try:
                raw = await get_purchase_order_history(mat, plant, s4=s4)
                po = _json.loads(raw)
                mat_result["poHistory"][plant] = {
                    "totalOrders": po.get("totalOrders", 0),
                    "avgPrice":    po.get("avgPrice", 0),
                    "currency":    po.get("currency", ""),
                    "orders":      po.get("orders", [])[:10],   # cap at 10 for UI
                    "error":       po.get("error", ""),
                }
            except Exception as e:
                mat_result["poHistory"][plant] = {"error": str(e), "totalOrders": 0, "orders": []}

        # ── BOM comparison (pairwise: plant[0] vs plant[i]) ──────────────────
        mat_result["bom"] = {}
        for i in range(1, len(plants)):
            key = f"{plants[0]}_vs_{plants[i]}"
            try:
                raw = await get_bom_cross_plant(mat, plants[0], plants[i], s4=s4)
                bom = _json.loads(raw)
                mat_result["bom"][key] = {
                    "plant1":         plants[0],
                    "plant2":         plants[i],
                    "componentCount": bom.get("componentCount", 0),
                    "components":     bom.get("components", [])[:50],  # cap
                    "error":          bom.get("error", ""),
                    "status":         bom.get("status", ""),
                }
            except Exception as e:
                mat_result["bom"][key] = {"error": str(e), "plant1": plants[0], "plant2": plants[i]}

        # ── Make/Buy classification ───────────────────────────────────────────
        try:
            p0 = mat_result["plants"].get(plants[0], {})
            p1 = mat_result["plants"].get(plants[1], {})
            # Use effective price (S→std, V→MAP) for the most accurate variance
            ep0 = float(p0.get("effectivePrice") or p0.get("standardPrice") or p0.get("movingAvgPrice") or 0)
            ep1 = float(p1.get("effectivePrice") or p1.get("standardPrice") or p1.get("movingAvgPrice") or 0)
            ptype = p0.get("procurementType", "")
            vendor_count = mat_result["infoRecords"].get("vendorCount", 0)
            po_count = mat_result["poHistory"].get(plants[0], {}).get("totalOrders", 0)

            var_pct = 0.0
            if ep0 and ep1 and max(ep0, ep1) > 0:
                var_pct = abs(ep0 - ep1) / max(ep0, ep1) * 100

            cl = classify_make_buy(
                material_number=mat,
                cost_variance_percent=var_pct,
                procurement_type=ptype,
                has_multiple_suppliers=vendor_count > 1,
            )
            # Enrich classification with justification details
            cl["justification"] = {
                "effectivePricePlant0":    ep0,
                "effectivePricePlant1":    ep1,
                "priceControlPlant0":      p0.get("priceControlLabel", ""),
                "priceControlPlant1":      p1.get("priceControlLabel", ""),
                "oldStdPricePlant0":       p0.get("oldStandardPrice", 0),
                "lastPriceChangePlant0":   p0.get("lastPriceChangeDate", ""),
                "stockQtyPlant0":          p0.get("totalStock", 0),
                "stockValuePlant0":        p0.get("totalStockValue", 0),
                "stockQtyPlant1":          p1.get("totalStock", 0),
                "stockValuePlant1":        p1.get("totalStockValue", 0),
                "vendorCount":             vendor_count,
                "poCount":                 po_count,
                "procurementType":         ptype,
                "procurementTypeName":     p0.get("procurementTypeName", ""),
            }
            mat_result["classification"] = cl
        except Exception as e:
            mat_result["classification"] = {"error": str(e)}

        # ── ADDON: Pairwise cost comparison across ALL plant combinations ────
        try:
            # Live FX rates — fetched from open.er-api.com (refreshed every 30 min)
            _live = get_live_fx_rates()
            FX = {
                ("EUR","USD"): _live["EUR_USD"], ("USD","EUR"): _live["USD_EUR"],
                ("EUR","INR"): _live["EUR_INR"], ("INR","EUR"): _live["INR_EUR"],
                ("USD","INR"): _live["USD_INR"], ("INR","USD"): _live["INR_USD"],
                ("GBP","USD"): _live["GBP_USD"], ("USD","GBP"): _live["USD_GBP"],
                ("GBP","INR"): _live["GBP_INR"], ("INR","GBP"): _live["INR_GBP"],
                ("CHF","USD"): _live["CHF_USD"], ("USD","CHF"): _live["USD_CHF"],
                ("CHF","INR"): _live["CHF_INR"],
            }

            def fx_convert(amount, from_curr, to_curr):
                """Convert amount from from_curr to to_curr. Returns None if rate unknown."""
                if from_curr == to_curr:
                    return round(amount, 2)
                rate = FX.get((from_curr, to_curr))
                if rate:
                    return round(amount * rate, 2)
                # try reverse
                rate_rev = FX.get((to_curr, from_curr))
                if rate_rev:
                    return round(amount / rate_rev, 2)
                return None

            plant_prices = {}
            for plant in plants:
                pd = mat_result["plants"].get(plant, {})
                ep = float(pd.get("effectivePrice") or pd.get("standardPrice") or pd.get("movingAvgPrice") or 0)
                plant_prices[plant] = {
                    "price": ep,
                    "currency": pd.get("currency", ""),
                    "invalid": pd.get("invalid", False),
                }

            pairs = []
            for i in range(len(plants)):
                for j in range(i + 1, len(plants)):
                    pa, pb = plants[i], plants[j]
                    da, db = plant_prices.get(pa, {}), plant_prices.get(pb, {})
                    epa, epb = da.get("price", 0), db.get("price", 0)
                    curr_a, curr_b = da.get("currency", ""), db.get("currency", "")
                    cross = bool(curr_a and curr_b and curr_a != curr_b)
                    var_pct = None
                    cheaper = None
                    # FX conversion fields (always in USD and INR for cross-currency)
                    price1_usd = price2_usd = price1_inr = price2_inr = None
                    fx_var_pct_usd = fx_var_pct_inr = None
                    fx_note = ""
                    if epa and epb and cross:
                        # Convert both to USD
                        price1_usd = fx_convert(epa, curr_a, "USD") if curr_a != "USD" else epa
                        price2_usd = fx_convert(epb, curr_b, "USD") if curr_b != "USD" else epb
                        # Convert both to INR
                        price1_inr = fx_convert(epa, curr_a, "INR") if curr_a != "INR" else epa
                        price2_inr = fx_convert(epb, curr_b, "INR") if curr_b != "INR" else epb
                        # Variance in USD
                        if price1_usd and price2_usd:
                            fx_var_pct_usd = round(abs(price1_usd - price2_usd) / max(price1_usd, price2_usd) * 100, 2)
                            cheaper = pa if price1_usd <= price2_usd else pb
                            var_pct = fx_var_pct_usd
                        if price1_inr and price2_inr:
                            fx_var_pct_inr = round(abs(price1_inr - price2_inr) / max(price1_inr, price2_inr) * 100, 2)
                        fx_note = f"Indicative rates: 1 {curr_a}={FX.get((curr_a,'USD'), '?')} USD / 1 {curr_b}={FX.get((curr_b,'USD'), '?')} USD"
                    elif epa and epb and not cross:
                        var_pct = round(abs(epa - epb) / max(epa, epb) * 100, 2)
                        cheaper = pa if epa <= epb else pb
                    pairs.append({
                        "plant1": pa, "plant2": pb,
                        "price1": epa, "price2": epb,
                        "currency1": curr_a, "currency2": curr_b,
                        "crossCurrency": cross,
                        "variancePct": var_pct,
                        "cheaperPlant": cheaper,
                        "currencyNote": f"Cross-currency: {pa}={curr_a}, {pb}={curr_b}" if cross else "",
                        # FX normalised values
                        "price1USD": price1_usd,
                        "price2USD": price2_usd,
                        "price1INR": price1_inr,
                        "price2INR": price2_inr,
                        "variancePctUSD": fx_var_pct_usd,
                        "variancePctINR": fx_var_pct_inr,
                        "fxNote": fx_note,
                    })

            # Best plant = lowest USD-normalised price across all valid plants
            best_plant, best_reasoning = None, ""
            valid = [(p, v["price"], v["currency"]) for p, v in plant_prices.items()
                     if v["price"] > 0 and not v.get("invalid")]
            if valid:
                # Convert every plant price to USD for fair comparison
                usd_prices = []
                for plant, price, curr in valid:
                    usd = fx_convert(price, curr, "USD") if curr != "USD" else price
                    if usd:
                        usd_prices.append((plant, usd, price, curr))
                if usd_prices:
                    best_plant, best_usd, best_orig, best_curr = min(usd_prices, key=lambda x: x[1])
                    best_reasoning = (
                        f"Lowest cost: {best_orig} {best_curr} = {best_usd} USD "
                        f"(FX-normalised comparison across all plants)"
                    )
                else:
                    # fallback: same-currency group
                    groups: dict = {}
                    for plant, price, curr in valid:
                        groups.setdefault(curr, []).append((plant, price))
                    top_curr = max(groups, key=lambda c: len(groups[c]))
                    grp = groups[top_curr]
                    best_plant, best_price = min(grp, key=lambda x: x[1])
                    best_reasoning = f"Lowest price ({best_price} {top_curr}) among same-currency plants"

            mat_result["pairwiseComparisons"] = {
                "pairs": pairs,
                "bestPlant": best_plant,
                "bestPlantReasoning": best_reasoning,
            }
        except Exception as _e:
            mat_result["pairwiseComparisons"] = {"error": str(_e), "pairs": []}
        # ── END ADDON ────────────────────────────────────────────────────────

        out[mat] = mat_result

    return JSONResponse({"status": "success", "results": out})


# ═══════════════════════════════════════════════════════════════════════════════
#  /api/chat  — conversational LLM agent with thread persistence
# ═══════════════════════════════════════════════════════════════════════════════
async def api_chat(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    message    = body.get("message", "").strip()
    context_id = body.get("contextId") or str(uuid.uuid4())
    # BOMA is INDEPENDENT — ignore left-panel context entirely.
    # BOMA fetches its own live data from SAP CIN_100 for any material/plant.
    # context = body.get("context", "")  # intentionally ignored

    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    # v2.0: All preprocessing (entity extraction, query understanding, planning)
    # happens inside SampleAgent.stream() via the new pipeline.
    # main.py passes the raw message directly — no injection, no preprocessing.
    full_msg = message

    exc   = get_executor()
    tools = [*exc.bom_tools, *exc.skill_tools]

    try:
        result = await exc.agent.invoke(full_msg, context_id, tools=tools)
        reply = result.message

        # v2.0: response_validator.strip_banned_phrases() is called inside agent.py
        # Keep a server-level safety net here just in case
        from response_validator import strip_banned_phrases
        _BANNED_CHECK = [
            "please run a go analysis", "click go to fetch",
            "add materials and plants, then click go",
            "run a go analysis first", "click go",
        ]
        if any(p in reply.lower() for p in _BANNED_CHECK):
            import logging as _logging
            _logging.getLogger(__name__).warning("BANNED PHRASE leaked to api_chat — stripping: ctx=%s", context_id)
            reply = strip_banned_phrases(reply)
            if not reply.strip():
                reply = "I'm ready to help. Please ask me about any SAP material or BOM."

        # Append performance metadata (time + tokens) to every response
        time_s = result.time_taken_ms / 1000
        if result.tokens_used > 0:
            t_in  = result.tokens_in
            t_out = result.tokens_out
            t_tot = result.tokens_used
            if t_in > 0 and t_out > 0:
                reply += (
                    f"\n\n---\n⏱ **{time_s:.1f}s** | "
                    f"In: ~**{t_in:,}** · Out: ~**{t_out:,}** · Total: ~**{t_tot:,}** tokens"
                )
            else:
                reply += f"\n\n---\n⏱ **{time_s:.1f}s** | ~**{t_tot:,}** tokens"
        else:
            reply += f"\n\n---\n⏱ **{time_s:.1f}s**"

        return JSONResponse({
            "reply":     reply,
            "status":    result.status,
            "contextId": context_id,
        })
    except Exception as e:
        logger.exception("api_chat error")
        return JSONResponse({"error": str(e), "contextId": context_id}, status_code=500)


async def api_feedback(request: Request) -> JSONResponse:
    """
    POST /api/feedback  — store user 👍/👎 vote for continuous improvement.
    Body: { question, answer, vote: "up"|"down" }
    Persisted to disk and injected as few-shot examples into every subsequent chat.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    question = body.get("question", "").strip()
    answer   = body.get("answer", "").strip()
    vote     = body.get("vote", "").strip().lower()

    if not question or vote not in ("up", "down"):
        return JSONResponse({"error": "question and vote ('up'|'down') are required"}, status_code=400)

    record_feedback(question, answer, vote)
    stats = get_feedback_stats()
    return JSONResponse({
        "status": "recorded",
        "vote": vote,
        "totalFeedback": stats["total"],
        "approvalRate": stats["approvalRate"],
        "questionType": _detect_question_type(question),
        "message": (
            "✅ Great! I'll use this answer as a training example for similar questions."
            if vote == "up" else
            "📝 Got it. I'll avoid this pattern for similar questions."
        ),
    })


async def api_feedback_stats(request: Request) -> JSONResponse:
    """GET /api/feedback/stats — return training statistics."""
    stats = get_feedback_stats()
    examples = get_training_examples()
    return JSONResponse({
        "stats": stats,
        "topExamples": [
            {"question": e["question"][:100], "type": e["question_type"], "votes": e["votes"]}
            for e in examples[:10]
        ],
    })


async def api_bom_explosion(request: Request) -> JSONResponse:
    """
    POST /api/bom-explosion
    Body: { material, plant }
    Returns multi-level BOM explosion with prices for the drill-down tree.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    material = body.get("material", "").strip().upper()
    plant    = body.get("plant", "").strip()
    if not material or not plant:
        return JSONResponse({"error": "material and plant required"}, status_code=400)

    exc = get_executor()
    s4  = exc.agent.s4_client

    try:
        from tools import get_bom_explosion, get_material_valuation, get_material_procurement_type
        import json as _j

        async def enrich_node(mat: str, plnt: str, qty: float, depth: int, visited: set) -> dict:
            """Recursively build BOM tree node with price + sub-components."""
            key = (mat, plnt)
            node = {"material": mat, "plant": plnt, "quantity": qty, "depth": depth, "children": []}

            # Get valuation
            try:
                val_raw = await get_material_valuation(mat, plnt, s4=s4)
                val = _j.loads(val_raw)
                node["price"]        = float(val.get("effectivePrice") or val.get("standardPrice") or val.get("movingAveragePrice") or 0)
                node["currency"]     = val.get("currency", "")
                node["priceControl"] = val.get("priceControlLabel", "")
                node["totalCost"]    = round(node["price"] * qty, 2)
            except Exception:
                node["price"] = 0; node["currency"] = ""; node["priceControl"] = ""; node["totalCost"] = 0

            # Get procurement type
            try:
                proc_raw = await get_material_procurement_type(mat, plnt, s4=s4)
                proc = _j.loads(proc_raw)
                node["procType"]     = proc.get("procurementType", "")
                node["procTypeName"] = proc.get("procurementTypeName", "")
            except Exception:
                node["procType"] = ""; node["procTypeName"] = ""

            # Get BOM children (max depth 5)
            if depth < 5 and key not in visited:
                visited = visited | {key}
                try:
                    bom_raw = await get_bom_explosion(mat, plnt, "1", "single", s4=s4)
                    bom = _j.loads(bom_raw)
                    for comp in bom.get("components", []):
                        comp_mat = comp.get("componentMaterial", "")
                        comp_qty = float(comp.get("quantity", 1) or 1) * qty
                        if comp_mat:
                            child = await enrich_node(comp_mat, plnt, comp_qty, depth + 1, visited)
                            child["description"] = comp.get("componentDescription", "")
                            child["unit"] = comp.get("unit", "EA")
                            child["bomNumber"] = comp.get("bomNumber", "")
                            node["children"].append(child)
                except Exception:
                    pass

            return node

        tree = await enrich_node(material, plant, 1.0, 0, set())
        return JSONResponse({"material": material, "plant": plant, "tree": tree, "status": "success"})

    except Exception as e:
        logger.exception("api_bom_explosion error")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_plants(request: Request) -> JSONResponse:
    """GET /api/plants — return list of known plants from SAP, with optional ?q= wildcard search."""
    exc = get_executor()
    s4  = exc.agent.s4_client
    q   = request.query_params.get("q", "").strip().upper()
    try:
        params: dict = {"$select": "Plant,PlantName", "$top": "200", "$format": "json"}
        if q:
            # Support wildcard: 17* → startswith filter
            base = q.rstrip("*").rstrip("?")
            if base:
                params["$filter"] = f"startswith(Plant,'{base}')"
        result = await s4.get("sap/opu/odata/sap/API_PLANT_SRV_0001/A_Plant", params=params)
        items = result.get("results", [])
        plants = [{"value": i["Plant"], "label": f"{i['Plant']} — {i.get('PlantName','')}"} for i in items if i.get("Plant")]
        return JSONResponse({"plants": plants})
    except Exception as e:
        logger.warning("api_plants error: %s", e)
        return JSONResponse({"plants": []})


async def api_materials(request: Request) -> JSONResponse:
    """GET /api/materials — live wildcard search. Tries multiple strategies to find materials."""
    exc = get_executor()
    s4  = exc.agent.s4_client
    q   = request.query_params.get("q", "").strip().upper()

    # Build the product filter part (no language constraint yet)
    def _product_filter(q_str: str) -> str:
        if not q_str:
            return ""
        clean = q_str.replace('*', '').replace('?', '').strip()
        if not clean:
            return ""
        if q_str.startswith('*') and q_str.endswith('*'):
            return f"substringof('{clean}',Product)"
        elif q_str.startswith('*'):
            return f"endswith(Product,'{clean}')"
        else:
            # default: startswith — works for both "F-10A" and "DEMO"
            return f"startswith(Product,'{clean}')"

    prod_f = _product_filter(q)
    mats: list = []

    try:
        # Strategy 1: A_ProductDescription with EN language
        en_filter = "Language eq 'EN'"
        if prod_f:
            en_filter = f"Language eq 'EN' and {prod_f}"
        r1 = await s4.get(
            "sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductDescription",
            params={"$select": "Product,Language,ProductDescription", "$filter": en_filter, "$top": "50"})
        items1 = r1.get("results", [])
        seen = set()
        for i in items1:
            p = i.get("Product", "")
            if p and p not in seen:
                seen.add(p)
                mats.append({"value": p, "label": f"{p} — {i.get('ProductDescription', '')}"})
    except Exception as e:
        logger.warning("api_materials strategy1 error: %s", e)

    # Strategy 2: if nothing found, try without language filter (gets any language)
    if not mats and prod_f:
        try:
            r2 = await s4.get(
                "sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductDescription",
                params={"$select": "Product,Language,ProductDescription", "$filter": prod_f, "$top": "50"})
            items2 = r2.get("results", [])
            seen = set()
            for i in items2:
                p = i.get("Product", "")
                if p and p not in seen:
                    seen.add(p)
                    mats.append({"value": p, "label": f"{p} — {i.get('ProductDescription', '')}"})
        except Exception as e:
            logger.warning("api_materials strategy2 error: %s", e)

    # Strategy 3: A_Product directly (no description, just product number)
    if not mats and prod_f:
        try:
            r3 = await s4.get(
                "sap/opu/odata/sap/API_PRODUCT_SRV/A_Product",
                params={"$select": "Product,ProductType,BaseUnit", "$filter": prod_f, "$top": "50"})
            items3 = r3.get("results", [])
            for i in items3:
                p = i.get("Product", "")
                if p:
                    mats.append({"value": p, "label": f"{p} — ({i.get('ProductType', 'MATERIAL')})"})
        except Exception as e:
            logger.warning("api_materials strategy3 error: %s", e)

    # Strategy 4: exact match check — if q looks like a full material number, verify it exists
    if not mats and q and '*' not in q and '?' not in q and len(q) >= 2:
        try:
            r4 = await s4.get(
                "sap/opu/odata/sap/API_PRODUCT_SRV/A_Product",
                params={"$select": "Product,ProductType", "$filter": f"Product eq '{q}'", "$top": "1"})
            items4 = r4.get("results", [])
            for i in items4:
                p = i.get("Product", "")
                if p:
                    mats.append({"value": p, "label": p})
        except Exception as e:
            logger.warning("api_materials strategy4 error: %s", e)

    return JSONResponse({"materials": mats})


# ═══════════════════════════════════════════════════════════════════════════════
#  /api/material-analyse  — NEW: single-material analysis for UI v28.07.2026(1)
#  Accepts { material, plant } — plant optional (discovers all if omitted)
#  Returns { plants, plantData, bomData, bomTree, supplierData, poData }
# ═══════════════════════════════════════════════════════════════════════════════
async def api_material_analyse(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    import json as _j

    # ── Handle searchOnly (material search dropdown) ──────────────────────────
    if body.get("searchOnly"):
        pattern = (body.get("search") or "").strip().upper()
        exc2 = get_executor()
        s4b  = exc2.agent.s4_client
        base = pattern.rstrip("*").rstrip("?").strip()
        if not base:
            return JSONResponse({"materials": []})
        mats2, seen2 = [], set()
        # S1: Product API
        try:
            r = await s4b.get("sap/opu/odata/sap/API_PRODUCT_SRV/A_Product",
                params={"$filter": f"startswith(Product,'{base}')", "$select": "Product", "$top": "20"})
            for i in r.get("results", []):
                p = i.get("Product","")
                if p and p not in seen2: seen2.add(p); mats2.append(p)
        except Exception: pass
        # S2: BOM table — catches F-10A, F-10B etc.
        if not mats2:
            try:
                r = await s4b.get("sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV_0002/A_BillOfMaterial",
                    params={"$filter": f"startswith(Material,'{base}')", "$select": "Material", "$top": "20"})
                for i in r.get("results", []):
                    p = i.get("Material","")
                    if p and p not in seen2: seen2.add(p); mats2.append(p)
            except Exception: pass
        # S3: ProductDescription
        if not mats2:
            try:
                r = await s4b.get("sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductDescription",
                    params={"$filter": f"Language eq 'EN' and startswith(Product,'{base}')",
                            "$select": "Product", "$top": "20"})
                for i in r.get("results", []):
                    p = i.get("Product","")
                    if p and p not in seen2: seen2.add(p); mats2.append(p)
            except Exception: pass
        # S4: hardcoded known CIN_100 materials
        _KNOWN_MATS = ["F-10A","F-10B","F-20A","DEMOCAR","DEMOBODY","SEAT","SEATCOVER","SEATCUSHION","SEATDESIGN"]
        if not mats2:
            mats2 = [m for m in _KNOWN_MATS if m.startswith(base)]
        logger.info("searchOnly '%s' -> %s", base, mats2)
        return JSONResponse({"materials": mats2})

    # ── Log entire body for debugging ────────────────────────────────────────
    logger.info("api_material_analyse body keys=%s body=%s", list(body.keys()), str(body)[:300])

    # ── Parse material — handle ALL possible formats ──────────────────────────
    material = ""
    for key in ["material", "Material", "MATERIAL"]:
        if body.get(key):
            material = str(body[key]).strip().upper()
            break
    if not material:
        for key in ["materials", "Materials", "MATERIALS"]:
            val = body.get(key)
            if val:
                if isinstance(val, list) and val:
                    material = str(val[0]).strip().upper()
                elif isinstance(val, str):
                    material = val.strip().upper()
                break

    # ── Parse plants — handle ALL possible formats ────────────────────────────
    plants_from_ui: list[str] = []
    for key in ["plants", "Plants", "PLANTS"]:
        val = body.get(key)
        if val:
            if isinstance(val, list):
                plants_from_ui = [str(p).strip() for p in val if str(p).strip()]
            elif isinstance(val, str):
                plants_from_ui = [p.strip() for p in val.replace(",", " ").split() if p.strip()]
            break
    if not plants_from_ui:
        for key in ["plant", "Plant", "PLANT"]:
            val = body.get(key)
            if val:
                if isinstance(val, list):
                    plants_from_ui = [str(p).strip() for p in val if str(p).strip()]
                elif isinstance(val, str):
                    plants_from_ui = [p.strip() for p in str(val).replace(",", " ").split() if p.strip()]
                break

    logger.info("Parsed material='%s' plants=%s", material, plants_from_ui)
    plant_filter = ",".join(plants_from_ui)

    # plant_filter_list = what the user explicitly asked for
    plant_filter_list = plants_from_ui  # already parsed above

    if not material:
        return JSONResponse({"error": "material is required"}, status_code=400)

    exc = get_executor()
    s4  = exc.agent.s4_client

    # ── Step 1: Discover / validate plants for this material ───────────────────
    found_plants = []

    # Always discover all plants from SAP first
    discovered_plants = []

    # Strategy 1: Standard product plant table
    for svc_path, filt_key in [
        ("sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductPlant", "Product"),
        ("sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductValuation", "Product"),
    ]:
        try:
            r = await s4.get(svc_path, params={
                "$filter": f"{filt_key} eq '{material}'",
                "$select": "Plant",
                "$top": "100",
            })
            items = r.get("results", [])
            if items:
                discovered_plants = list(dict.fromkeys(i.get("Plant", "") for i in items if i.get("Plant")))
                break
        except Exception as e:
            logger.warning("plant discovery error for %s: %s", material, e)

    # Strategy 2: BOM table fallback — finds F-10A, F-10B, F-20A, DEMOCAR etc.
    if not discovered_plants:
        try:
            r = await s4.get(
                "sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV_0002/A_BillOfMaterial",
                params={
                    "$filter": f"Material eq '{material}'",
                    "$select": "Material,Plant",
                    "$top": "50",
                }
            )
            items = r.get("results", [])
            if items:
                discovered_plants = list(dict.fromkeys(i.get("Plant", "") for i in items if i.get("Plant")))
                logger.info("Plant discovery via BOM table for %s: %s", material, discovered_plants)
        except Exception as e:
            logger.warning("BOM plant discovery error for %s: %s", material, e)

    # Strategy 3: Material valuation direct — another fallback
    if not discovered_plants:
        try:
            r = await s4.get(
                "sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductValuation",
                params={
                    "$filter": f"Product eq '{material}'",
                    "$select": "Plant",
                    "$top": "50",
                }
            )
            items = r.get("results", [])
            if items:
                discovered_plants = list(dict.fromkeys(i.get("Plant", "") for i in items if i.get("Plant")))
                logger.info("Plant discovery via valuation for %s: %s", material, discovered_plants)
        except Exception as e:
            logger.warning("Valuation plant discovery error for %s: %s", material, e)

    # Strategy 4: MAST-based known materials — hardcoded CIN_100 lookup for
    # training/demo materials that don't appear in standard product tables
    _KNOWN_CIN100 = {
        "F-10A":   ["1010", "1710"],
        "F-10B":   ["1010", "1710"],
        "F-20A":   ["1010", "1710"],
        "DEMOCAR": ["1010", "1710", "1720"],
        "DEMOBODY":["1010", "1710", "1720"],
        "SEAT":    ["1010", "1710", "1720"],
        "SEATCOVER":   ["1010", "1710", "1720"],
        "SEATCUSHION": ["1010", "1710", "1720"],
        "SEATDESIGN":  ["1010", "1710", "1720"],
    }
    if not discovered_plants and material in _KNOWN_CIN100:
        discovered_plants = _KNOWN_CIN100[material]
        logger.info("Plant discovery via MAST table for known material %s: %s", material, discovered_plants)

    # Strategy 5: Probe common CIN_100 plants via valuation — last resort
    if not discovered_plants:
        _PROBE_PLANTS = ["1010", "1710", "1720", "1000", "1100", "2000"]
        working = []
        for probe_plant in _PROBE_PLANTS:
            try:
                raw = await get_material_valuation(material, probe_plant, s4=s4)
                import json as _jj
                v = _jj.loads(raw)
                if not v.get("error") and (v.get("standardPrice") or v.get("movingAveragePrice") or v.get("effectivePrice")):
                    working.append(probe_plant)
                    logger.info("Probe found %s at plant %s", material, probe_plant)
            except Exception:
                pass
        if working:
            discovered_plants = working
            logger.info("Plant discovery via valuation probe for %s: %s", material, discovered_plants)

    if plant_filter_list:
        # ── User explicitly typed plants — ALWAYS trust them ──────────────────
        # Never block user-specified plants due to failed discovery.
        # If user typed 1010 and 1710, use exactly those.
        found_plants = plant_filter_list
        logger.info("Using user-specified plants for %s: %s", material, found_plants)
    elif discovered_plants:
        found_plants = discovered_plants
    else:
        # Nothing found — return helpful error
        return JSONResponse({
            "plants": [],
            "plantData": {},
            "bomData": {},
            "bomTree": None,
            "supplierData": {},
            "poData": {},
            "error": f"Material '{material}' not found. Try entering plants manually e.g. 1010, 1710.",
        })

    # ── Step 2: Per-plant data ─────────────────────────────────────────────────
    plant_data = {}
    for plant in found_plants:
        pd = {}
        # Valuation
        try:
            raw = await get_material_valuation(material, plant, s4=s4)
            v = _j.loads(raw)
            if not v.get("error"):
                pd.update({
                    "plantName":           plant,
                    "priceControl":        v.get("priceControl",""),
                    "priceControlLabel":   v.get("priceControlLabel",""),
                    "standardPrice":       v.get("standardPrice", 0),
                    "movingAvgPrice":      v.get("movingAveragePrice", 0),
                    "effectivePrice":      v.get("effectivePrice", 0),
                    "currency":            v.get("currency",""),
                    "priceUnit":           v.get("priceUnit", 1),
                    "valuationClass":      v.get("valuationClass",""),
                    "totalStock":          v.get("totalStock", 0),
                    "totalStockValue":     v.get("totalStockValue", 0),
                })
        except Exception as e:
            pd["valuationError"] = str(e)

        # Procurement type
        try:
            raw = await get_material_procurement_type(material, plant, s4=s4)
            p_ = _j.loads(raw)
            if not p_.get("error"):
                pd.update({
                    "procurementType":     p_.get("procurementType",""),
                    "procurementTypeName": p_.get("procurementTypeName",""),
                    "mrpType":             p_.get("mrpType",""),
                    "lotSize":             p_.get("lotSize",""),
                    "reorderPoint":        p_.get("reorderPoint", 0),
                })
        except Exception as e:
            pd["procurementError"] = str(e)

        # ── Material master: type, group, baseUnit from A_Product ──────────────
        try:
            rp = await s4.get(
                "sap/opu/odata/sap/API_PRODUCT_SRV/A_Product",
                params={"$filter": f"Product eq '{material}'",
                        "$select": "Product,ProductType,ProductGroup,BaseUnit",
                        "$top": "1", "$format": "json"},
            )
            rp_items = rp.get("results", []) if not rp.get("error") else []
            if rp_items:
                pd["materialType"]  = rp_items[0].get("ProductType", "")
                pd["materialGroup"] = rp_items[0].get("ProductGroup", "")
                pd["baseUnit"]      = rp_items[0].get("BaseUnit", "")
        except Exception as e:
            logger.warning("A_Product fetch error mat=%s plant=%s: %s", material, plant, e)

        # ── Description from A_ProductDescription (EN first, any language fallback) ─
        if not pd.get("description"):
            for lang_filter in (f"Language eq 'EN' and Product eq '{material}'",
                                f"Product eq '{material}'"):
                try:
                    rd = await s4.get(
                        "sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductDescription",
                        params={"$filter": lang_filter, "$top": "1", "$format": "json"},
                    )
                    rd_items = rd.get("results", []) if not rd.get("error") else []
                    if rd_items:
                        pd["description"] = rd_items[0].get("ProductDescription", "")
                        break
                except Exception:
                    pass

        # Profit center from plant data
        try:
            r = await s4.get(
                "sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductPlant",
                params={"$filter": f"Product eq '{material}' and Plant eq '{plant}'", "$top": "1"},
            )
            items = r.get("results", [])
            if items:
                pd["profitCenter"] = items[0].get("ProfitCenter","")
                pd["plantName"]    = items[0].get("PlantName", plant)
        except Exception:
            pass

        # ── Mark plant as INVALID if no useful data came back from SAP ────────
        # This fires when user types a wrong plant code (e.g. '9999') OR a real
        # plant where this specific material is not maintained.
        _has_any_data = (
            pd.get("effectivePrice") or pd.get("standardPrice") or pd.get("movingAvgPrice") or
            pd.get("procurementType") or pd.get("description") or
            pd.get("materialType")   or pd.get("profitCenter")
        )
        if not _has_any_data:
            pd["invalid"] = True
            pd["invalidReason"] = (
                f"Material '{material}' doesn't exist in plant '{plant}' in SAP CIN_100. "
                f"Please check that both the material number and plant code are correct."
            )
            logger.info("Marked plant=%s invalid for material=%s — no data in SAP", plant, material)

        plant_data[plant] = pd

    # ── If ALL user-specified plants are invalid → surface a clear error ───────
    # This covers: wrong material entirely, OR every plant code the user typed is wrong.
    _valid_plants = [p for p in found_plants if not plant_data.get(p, {}).get("invalid")]
    if not _valid_plants:
        _bad = ", ".join(found_plants)
        _err = (
            f"Material '{material}' doesn't exist in SAP CIN_100, "
            f"or none of the entered plants ({_bad}) contain this material. "
            f"Please verify both the material number and the plant codes."
        )
        logger.warning("All plants invalid for material=%s plants=%s", material, found_plants)
        return JSONResponse({
            "plants":      found_plants,
            "plantData":   plant_data,
            "bomData":     {},
            "bomTree":     None,
            "supplierData":{},
            "poData":      {},
            "error":       _err,
        })

    # ── Step 3: BOM Numbers ───────────────────────────────────────────────────
    # Try _0002 first (confirmed working for CIN_100 via plant-discovery Strategy 2),
    # then base service as fallback.
    # Pass 1 uses confirmed-safe $select; Pass 2 uses no $select as last resort.
    _BOM_SERVICES = [
        "sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV_0002/A_BillOfMaterial",  # confirmed working
        "sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV/A_BillOfMaterial",        # fallback
    ]
    bom_data = {}
    for plant in found_plants:
        # Skip invalid plants — no point querying SAP for a non-existent material/plant
        if plant_data.get(plant, {}).get("invalid"):
            bom_data[plant] = []
            continue

        items = []

        # Pass 1 — minimal confirmed $select (same fields used in get_bom_headers tool)
        for bom_svc in _BOM_SERVICES:
            try:
                r = await s4.get(bom_svc, params={
                    "$filter": f"Material eq '{material}' and Plant eq '{plant}'",
                    "$select": "BillOfMaterial,BillOfMaterialCategory,BillOfMaterialVariant,BillOfMaterialVersion,Plant,Material",
                    "$top": "20", "$format": "json",
                })
                items = r.get("results", []) if not r.get("error") else []
                if items:
                    logger.info("BOM P1 via %s for %s/%s: %d rows", bom_svc, material, plant, len(items))
                    break
            except Exception as e:
                logger.warning("BOM P1 error svc=%s mat=%s plant=%s: %s", bom_svc, material, plant, e)

        # Pass 2 — no $select: SAP returns every field it knows
        if not items:
            for bom_svc in _BOM_SERVICES:
                try:
                    r = await s4.get(bom_svc, params={
                        "$filter": f"Material eq '{material}' and Plant eq '{plant}'",
                        "$top": "20", "$format": "json",
                    })
                    items = r.get("results", []) if not r.get("error") else []
                    if items:
                        logger.info("BOM P2 (no select) via %s for %s/%s: %d rows", bom_svc, material, plant, len(items))
                        break
                except Exception as e:
                    logger.warning("BOM P2 error svc=%s mat=%s plant=%s: %s", bom_svc, material, plant, e)

        bom_data[plant] = [
            {
                "bomNumber":      i.get("BillOfMaterial", ""),
                "alternativeBom": i.get("BillOfMaterialVariant", "") or i.get("BillOfMaterialVariantNumber", "1"),
                "bomUsage":       i.get("BOMUsage", "") or i.get("BillOfMaterialUsage", "") or i.get("BillOfMaterialVariantUsage", "1"),
                "validFrom":      (i.get("ValidityStartDate") or i.get("BillOfMaterialHeaderValidityStartDate") or "")[:10],
                "validTo":        (i.get("ValidityEndDate")   or i.get("BillOfMaterialHeaderValidityEndDate")   or "")[:10],
                "status":         i.get("BillOfMaterialStatus", "") or i.get("BOMStatus", ""),
                "baseQuantity":   i.get("HeaderBaseQuantity", "")   or i.get("BillOfMaterialBaseQuantity", 1),
                "baseUnit":       i.get("HeaderBaseUnit", "")       or i.get("BillOfMaterialBaseUoM", ""),
            }
            for i in items if i.get("BillOfMaterial")
        ]

    # ── Step 4: BOM Tree (multi-level, first plant only for performance) ───────
    bom_tree = None
    primary_plant = found_plants[0]
    try:
        from tools import get_bom_explosion

        async def _build_tree(mat, plnt, qty, depth, visited_set):
            if depth > 4 or (mat, plnt) in visited_set:
                return None
            visited_set = visited_set | {(mat, plnt)}
            node = {"material": mat, "plant": plnt, "quantity": qty, "depth": depth,
                    "description": "", "unit": "EA", "prices": {}, "children": [],
                    "procurementType": "", "cheapestPlant": None}

            # Get prices for ALL plants for this component
            for p in found_plants:
                try:
                    raw = await get_material_valuation(mat, p, s4=s4)
                    v = _j.loads(raw)
                    if not v.get("error"):
                        eff = float(v.get("effectivePrice") or v.get("standardPrice") or v.get("movingAveragePrice") or 0)
                        if eff:
                            node["prices"][p] = {"price": eff, "currency": v.get("currency",""),
                                                  "inr": eff if v.get("currency") == "INR" else round(eff * 83.5, 2),
                                                  "usd": eff if v.get("currency") == "USD" else round(eff / 83.5, 2)}
                except Exception:
                    pass

            # Cheapest plant
            if node["prices"]:
                node["cheapestPlant"] = min(node["prices"], key=lambda p: node["prices"][p].get("inr", 9e9))

            # Procurement type
            try:
                raw = await get_material_procurement_type(mat, plnt, s4=s4)
                proc = _j.loads(raw)
                node["procurementType"] = proc.get("procurementType","")
            except Exception:
                pass

            # Sub-components
            try:
                bom_raw = await get_bom_explosion(mat, plnt, "1", "single", s4=s4)
                bom = _j.loads(bom_raw)
                for comp in bom.get("components", [])[:15]:
                    comp_mat = comp.get("componentMaterial","")
                    if not comp_mat:
                        continue
                    child = await _build_tree(comp_mat, plnt, float(comp.get("quantity",1) or 1), depth+1, visited_set)
                    if child:
                        child["description"] = comp.get("componentDescription","")
                        child["unit"]        = comp.get("unit","EA")
                        node["children"].append(child)
            except Exception:
                pass

            return node

        bom_tree = await _build_tree(material, primary_plant, 1.0, 0, set())
        if bom_tree:
            # Add summary fields
            def _count_nodes(n):
                if not n: return 0
                return 1 + sum(_count_nodes(c) for c in n.get("children",[]))
            def _max_depth(n):
                if not n or not n.get("children"): return n.get("depth", 0) if n else 0
                return max(_max_depth(c) for c in n["children"])
            bom_tree["totalNodes"] = _count_nodes(bom_tree) - 1
            bom_tree["maxDepth"]   = _max_depth(bom_tree)
    except Exception as e:
        logger.warning("BOM tree error: %s", e)

    # ── Step 5: Supplier data from info records ────────────────────────────────
    supplier_data = {}
    for plant in found_plants:
        try:
            raw = await get_purchase_info_records(material, plant, s4=s4)
            pir = _j.loads(raw)
            vendors = []
            for v in pir.get("infoRecords", []):
                vendors.append({
                    "supplierNo":    v.get("vendorNo",""),
                    "supplierName":  v.get("vendorName",""),
                    "price":         float(v.get("netPrice") or v.get("price") or 0),
                    "currency":      v.get("currency",""),
                    "priceUnit":     v.get("priceUnit",""),
                    "leadTime":      v.get("plannedDeliveryTime",""),
                    "incoterms":     v.get("incoterms",""),
                    "paymentTerms":  v.get("paymentTerms",""),
                    "preferred":     v.get("preferred", False),
                    "validFrom":     (v.get("validFrom","") or "")[:10],
                    "validTo":       (v.get("validTo","")   or "")[:10],
                })
            supplier_data[plant] = vendors
        except Exception as e:
            logger.warning("supplier data error plant=%s: %s", plant, e)
            supplier_data[plant] = []

    # ── Step 6: Purchase Orders ────────────────────────────────────────────────
    po_data = {}
    for plant in found_plants:
        try:
            raw = await get_purchase_order_history(material, plant, s4=s4)
            ph  = _j.loads(raw)
            pos = []
            for o in ph.get("orders", [])[:20]:
                pos.append({
                    "poNumber":     o.get("purchaseOrder","") or o.get("PurchaseOrder",""),
                    "poDate":       (o.get("orderDate","") or o.get("CreationDate","") or "")[:10],
                    "supplierName": o.get("supplierName","") or o.get("Supplier",""),
                    "quantity":     float(o.get("quantity",0) or o.get("OrderQuantity",0) or 0),
                    "unit":         o.get("unit","") or o.get("PurchaseOrderQuantityUnit",""),
                    "netValue":     float(o.get("netValue",0) or o.get("NetPriceAmount",0) or 0),
                    "currency":     o.get("currency","") or o.get("DocumentCurrency",""),
                    "deliveryDate": (o.get("deliveryDate","") or o.get("ScheduleLineDeliveryDate","") or "")[:10],
                    "status":       o.get("status","OPEN"),
                    "documentType": o.get("documentType","NB") or o.get("PurchasingDocumentType","NB"),
                    "material":     material,
                })
            po_data[plant] = pos
        except Exception as e:
            logger.warning("PO data error plant=%s: %s", plant, e)
            po_data[plant] = []

    return JSONResponse({
        "material":     material,
        "plant":        plant_filter or None,
        "plants":       found_plants,
        "plantData":    plant_data,
        "bomData":      bom_data,
        "bomTree":      bom_tree,
        "supplierData": supplier_data,
        "poData":       po_data,
        "status":       "success",
    })


async def api_debug_valuation(request: Request) -> JSONResponse:
    """
    GET /api/debug/valuation?material=F-10A&plant=1010&token=<token>
    Shows RAW response from every valuation/product API so we know exactly
    which fields SAP returns for this material+plant combination.
    Pass token= to test with real user credentials.
    """
    material = request.query_params.get("material", "F-10A").strip().upper()
    plant    = request.query_params.get("plant", "1010").strip()
    token    = request.query_params.get("token", "").strip()
    # also check Authorization header
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    # Define out FIRST before referencing it
    out = {"material": material, "plant": plant, "calls": {}}
    if token:
        creds = resolve_token(token)
        if creds:
            set_request_auth(creds["username"], creds["password"])
            out["auth_status"] = f"credentials injected for user: {creds['username']}"
        else:
            out["auth_status"] = "token not found in store — re-login first"
    else:
        out["auth_status"] = "no token — pass ?token=YOUR_TOKEN or Authorization: Bearer header"
    exc = get_executor()
    s4  = exc.agent.s4_client

    calls = [
        ("A_ProductValuation_Product",
         f"sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductValuation",
         {"$filter": f"Product eq '{material}' and ValuationArea eq '{plant}'", "$top": "1", "$format": "json"}),
        ("A_ProductValuation_Material",
         f"sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductValuation",
         {"$filter": f"Material eq '{material}' and ValuationArea eq '{plant}'", "$top": "1", "$format": "json"}),
        ("A_MaterialValuation",
         f"sap/opu/odata/sap/API_MATERIAL_VALUATION_SRV/A_MaterialValuation",
         {"$filter": f"Material eq '{material}' and ValuationArea eq '{plant}'", "$top": "1", "$format": "json"}),
        ("A_Product",
         f"sap/opu/odata/sap/API_PRODUCT_SRV/A_Product",
         {"$filter": f"Product eq '{material}'", "$top": "1", "$format": "json"}),
        ("A_ProductDescription_EN",
         f"sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductDescription",
         {"$filter": f"Product eq '{material}' and Language eq 'EN'", "$top": "1", "$format": "json"}),
        ("A_ProductPlant",
         f"sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductPlant",
         {"$filter": f"Product eq '{material}' and Plant eq '{plant}'", "$top": "1", "$format": "json"}),
    ]

    for name, path, params in calls:
        try:
            r = await s4.get(path, params=params)
            items = r.get("results", []) if not r.get("error") else []
            out["calls"][name] = {
                "error": r.get("error"),
                "error_msg": r.get("message", "")[:300] if r.get("error") else None,
                "row_count": len(items),
                "first_row_keys": list(items[0].keys()) if items else [],
                "first_row": items[0] if items else None,
            }
        except Exception as e:
            out["calls"][name] = {"exception": str(e)}

    return JSONResponse(out)


async def api_bom_debug(request: Request) -> JSONResponse:
    """
    GET /api/debug/bom?material=DEMOCAR&plant=1710&bom=00000381
    Raw SAP BOM API probe — shows exactly what each endpoint returns.
    """
    from s4hana_client import Client
    material = request.query_params.get("material", "DEMOCAR")
    plant    = request.query_params.get("plant", "1710")
    bom_no   = request.query_params.get("bom", "00000381")
    cat      = request.query_params.get("cat", "M")

    BOM_ROOT     = "/sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV_0002"
    BOM_ROOT_ALT = "/sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV"

    client = Client()
    out = {}

    # 1. Metadata — what entity sets exist?
    for svc_key, svc in [("_0002", BOM_ROOT), ("base", BOM_ROOT_ALT)]:
        try:
            entities = await client.get_service_entities(svc)
            props_item = await client.get_entity_set_properties(svc, "A_BillOfMaterialItem") if "A_BillOfMaterialItem" in entities else {}
            out[f"metadata_{svc_key}"] = {"entities": entities, "item_keys": props_item.get("keys",[]), "item_all_props": props_item.get("all",[])}
        except Exception as e:
            out[f"metadata_{svc_key}"] = {"error": str(e)}

    # 2. Header with $expand — raw HTTP call bypassing client for diagnosis
    import httpx as _httpx
    dest = await client.destination()
    _base = dest.url
    _auth = (dest.username, dest.password) if dest.username else None
    _sap_client = dest.sap_client or ""
    _proxy_type = dest.proxy_type.lower()

    for svc_key, svc in [("_0002", BOM_ROOT), ("base", BOM_ROOT_ALT)]:
        _params = {
            "$filter": f"Material eq '{material}' and Plant eq '{plant}'",
            "$expand": "to_BillOfMaterialItem",
            "$top": "5", "$format": "json",
        }
        if _sap_client:
            _params["sap-client"] = _sap_client
        _url = f"{_base}{svc}/A_BillOfMaterial"
        _kwargs = {"timeout": 20.0}
        _extra_headers = {}
        if _proxy_type == "onpremise":
            try:
                proxy_url, proxy_hdr = await client._resolver.proxy_settings()
                _kwargs["proxy"] = proxy_url
                _extra_headers = proxy_hdr
            except Exception as pe:
                out[f"proxy_error_{svc_key}"] = str(pe)
        try:
            async with _httpx.AsyncClient(**_kwargs) as hc:
                raw_r = await hc.get(_url, auth=_auth, params=_params,
                                      headers={"Accept": "application/json", **_extra_headers})
            raw_status = raw_r.status_code
            import json as _json
            raw_body = None
            parse_error = None
            raw_body_text = ""
            try:
                # Read content first to avoid streaming issues
                raw_content = raw_r.content
                raw_body_text = raw_content.decode("utf-8", "replace")
                raw_body = _json.loads(raw_body_text)
            except Exception as _pe:
                parse_error = str(_pe)
                try:
                    raw_body = _json.loads(raw_r.text)
                except Exception:
                    pass
            # Extract results — handle d.results wrapper
            hdrs = []
            if raw_body and isinstance(raw_body, dict):
                inner = raw_body.get("d", raw_body)
                if isinstance(inner, dict):
                    hdrs = inner.get("results", [])
            out[f"expand_{svc_key}"] = {
                "status_code": raw_status,
                "header_count": len(hdrs),
                "parse_error": parse_error,
                "body_type": type(raw_body).__name__,
                "body_keys": list(raw_body.keys()) if raw_body and isinstance(raw_body, dict) else [],
                "d_keys": list(raw_body.get("d", {}).keys()) if raw_body and isinstance(raw_body, dict) and "d" in raw_body else [],
                "headers": [
                    {
                        "BOM": h.get("BillOfMaterial"),
                        "Cat": h.get("BillOfMaterialCategory"),
                        "Var": h.get("BillOfMaterialVariant"),
                        "Plant": h.get("Plant"),
                        "has_expand_key": "to_BillOfMaterialItem" in h,
                        "items_count": len((h.get("to_BillOfMaterialItem") or {}).get("results", [])),
                        "items_sample": [
                            {"comp": i.get("BillOfMaterialComponent"), "qty": i.get("BillOfMaterialItemQuantity")}
                            for i in (h.get("to_BillOfMaterialItem") or {}).get("results", [])[:3]
                        ],
                    }
                    for h in hdrs[:3]
                ],
                "raw_body_preview": raw_body_text[:300],
            }
        except Exception as ex:
            out[f"expand_{svc_key}"] = {"error": str(ex)}

    # 3. Navigation property — all key combos
    nav_results = {}
    for svc_key, svc in [("_0002", BOM_ROOT), ("base", BOM_ROOT_ALT)]:
        for var in ("1", "01"):
            for ver in ("", "0", "00"):
                key = f"{svc_key}_var{var}_ver{repr(ver)}"
                path = (f"{svc}/A_BillOfMaterial("
                        f"BillOfMaterial='{bom_no}',"
                        f"BillOfMaterialCategory='{cat}',"
                        f"BillOfMaterialVariant='{var}',"
                        f"BillOfMaterialVersion='{ver}'"
                        f")/to_BillOfMaterialItem")
                r = await client.get(path, params={"$top": "20", "$format": "json"})
                items = r.get("results", []) if not r.get("error") else []
                nav_results[key] = {
                    "http": r.get("status_code"),
                    "error": r.get("message","")[:150] if r.get("error") else None,
                    "count": len(items),
                    "sample": items[:2]
                }
    out["navigation"] = nav_results

    # 4. A_BillOfMaterialItem direct filter
    filter_results = {}
    for svc_key, svc in [("_0002", BOM_ROOT), ("base", BOM_ROOT_ALT)]:
        for filt in [
            f"BillOfMaterial eq '{bom_no}'",
            f"BillOfMaterial eq '{bom_no}' and BillOfMaterialCategory eq '{cat}'",
            f"BillOfMaterial eq '{bom_no}' and BillOfMaterialCategory eq '{cat}' and BillOfMaterialVariant eq '1'",
            f"BillOfMaterial eq '{bom_no}' and BillOfMaterialCategory eq '{cat}' and BillOfMaterialVariant eq '01'",
        ]:
            fkey = f"{svc_key}_{filt[:40]}"
            r = await client.get(f"{svc}/A_BillOfMaterialItem",
                                  params={"$filter": filt, "$top": "20", "$format": "json"})
            items = r.get("results", []) if not r.get("error") else []
            filter_results[fkey] = {
                "http": r.get("status_code"),
                "error": r.get("message","")[:150] if r.get("error") else None,
                "count": len(items),
                "sample": items[:2]
            }
    out["filter_on_item"] = filter_results

    # 5. Unfiltered A_BillOfMaterialItem — any items at all?
    for svc_key, svc in [("_0002", BOM_ROOT), ("base", BOM_ROOT_ALT)]:
        r = await client.get(f"{svc}/A_BillOfMaterialItem",
                              params={"$top": "3", "$format": "json"})
        items = r.get("results", []) if not r.get("error") else []
        out[f"unfiltered_items_{svc_key}"] = {
            "error": r.get("message","")[:200] if r.get("error") else None,
            "count": len(items),
            "keys_of_first": list(items[0].keys()) if items else [],
            "sample": items[:2]
        }

    return JSONResponse(out)


# ═══════════════════════════════════════════════════════════════════════════════
#  App assembly
# ═══════════════════════════════════════════════════════════════════════════════
def build_app():  # noqa: C901
    skill = AgentSkill(
        id="bom-cost-optimization-agent",
        name="bom-cost-optimization-agent",
        description=(
            "AI-powered cross-plant BOM cost optimization: Make/Buy/Strategic Buy "
            "classification, procurement analysis, workflow triggers."
        ),
        tags=["bom", "cost", "optimization", "automotive"],
        examples=[
            "Compare BOM costs for F-10A between plants 1010 and 1710",
            "Which components should be Strategic Buy?",
        ],
    )
    agent_card = AgentCard(
        name="bom-cost-optimization-agent",
        description=(
            "AI-powered cross-plant BOM cost optimization for ABC Automotive. "
            "Classifies components as Make/Buy/Strategic Buy."
        ),
        url=os.environ.get("AGENT_PUBLIC_URL", f"http://{HOST}:{PORT}/"),
        version="1.0.0",
        default_input_modes=["text", "text/plain"],
        default_output_modes=["text", "text/plain"],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        skills=[skill],
    )
    a2a_server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=DefaultRequestHandler(
            agent_executor=get_executor(),
            task_store=InMemoryTaskStore(),
        ),
    )
    a2a_app = a2a_server.build()

    # Wrap with top-level Starlette that owns the REST routes + CORS
    # Pure ASGI dispatcher — most reliable way to add routes alongside A2A
    async def _health(_scope, _receive, send):
        body = b'{"ok":true}'
        await send({"type":"http.response.start","status":200,"headers":[[b"content-type",b"application/json"],[b"content-length",str(len(body)).encode()]]})
        await send({"type":"http.response.body","body":body})

    async def _handle_api(endpoint, scope, receive, send):
        request = Request(scope, receive)
        response = await endpoint(request)
        await response(scope, receive, send)

    async def _cors_wrap(app_fn, scope, receive, send):
        """Add CORS headers to any response."""
        if scope.get("method") == "OPTIONS" or (scope.get("type") == "http" and scope.get("method") == "OPTIONS"):
            body = b""
            await send({"type":"http.response.start","status":204,"headers":[
                [b"access-control-allow-origin",b"*"],
                [b"access-control-allow-methods",b"GET, POST, OPTIONS"],
                [b"access-control-allow-headers",b"*"],
                [b"content-length",b"0"],
            ]})
            await send({"type":"http.response.body","body":body})
            return

        # Wrap send to inject CORS headers
        started = False
        async def cors_send(message):
            nonlocal started
            if message["type"] == "http.response.start" and not started:
                started = True
                headers = list(message.get("headers", []))
                headers.append([b"access-control-allow-origin", b"*"])
                message = {**message, "headers": headers}
            await send(message)

        await app_fn(scope, receive, cors_send)

    # ── Static UI files directory ─────────────────────────────────────────────
    _UI_DIR = os.path.join(os.path.dirname(__file__), "ui")
    _ui_exists = os.path.isdir(_UI_DIR)
    logger.info("UI dir: %s  exists=%s", _UI_DIR, _ui_exists)

    async def _serve_static(path: str, scope, receive, send):
        """Serve a file from the ui/ folder."""
        import mimetypes
        full = os.path.join(_UI_DIR, path.lstrip("/"))
        if not os.path.isfile(full):
            # SPA fallback → index.html
            full = os.path.join(_UI_DIR, "index.html")
        if not os.path.isfile(full):
            await send({"type":"http.response.start","status":404,"headers":[[b"content-length",b"9"]]})
            await send({"type":"http.response.body","body":b"Not found"})
            return
        mime, _ = mimetypes.guess_type(full)
        mime = (mime or "application/octet-stream").encode()
        with open(full, "rb") as f:
            data = f.read()
        await send({"type":"http.response.start","status":200,"headers":[
            [b"content-type", mime],
            [b"content-length", str(len(data)).encode()],
            [b"cache-control", b"no-store"],
        ]})
        await send({"type":"http.response.body","body":data})

    async def root_asgi(scope, receive, send):
        if scope["type"] != "http":
            await a2a_app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")

        # ── Inject user SAP credentials for EVERY request ────────────────────
        # Extract Bearer token from Authorization header and resolve to
        # stored SAP username/password so every downstream API call authenticates.
        try:
            raw_headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth_bytes  = raw_headers.get(b"authorization", b"")
            auth_str    = auth_bytes.decode("utf-8", "replace") if auth_bytes else ""
            if auth_str.lower().startswith("bearer "):
                tok = auth_str[7:].strip()
                if tok:
                    creds = resolve_token(tok)
                    if creds:
                        set_request_auth(creds["username"], creds["password"])
                        logger.debug("[auth] credentials injected for token %s...", tok[:8])
        except Exception as _auth_err:
            logger.warning("[auth] credential injection failed: %s", _auth_err)

        if method == "OPTIONS" and path.startswith("/api/"):
            await send({"type":"http.response.start","status":204,"headers":[
                [b"access-control-allow-origin",b"*"],
                [b"access-control-allow-methods",b"POST, OPTIONS"],
                [b"access-control-allow-headers",b"content-type, authorization"],
                [b"content-length",b"0"],
            ]})
            await send({"type":"http.response.body","body":b""})
            clear_request_auth()
            return

        async def add_cors(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append([b"access-control-allow-origin", b"*"])
                message = {**message, "headers": headers}
            await send(message)

        if path == "/api/health":
            await _health(scope, receive, add_cors)
        elif path == "/api/plants":
            await _handle_api(api_plants, scope, receive, add_cors)
        elif path == "/api/materials":
            await _handle_api(api_materials, scope, receive, add_cors)
        elif path == "/api/analyse":
            # Handles ALL /api/analyse requests — both searchOnly and full analysis
            await _handle_api(api_material_analyse, scope, receive, add_cors)
        elif path == "/api/login":
            await _handle_api(api_login, scope, receive, add_cors)
        elif path == "/api/validate-credentials":
            await _handle_api(api_validate_credentials, scope, receive, add_cors)
        elif path == "/api/chat":
            await _handle_api(api_chat, scope, receive, add_cors)
        elif path == "/api/feedback":
            await _handle_api(api_feedback, scope, receive, add_cors)
        elif path == "/api/feedback/stats":
            await _handle_api(api_feedback_stats, scope, receive, add_cors)
        elif path == "/api/bom-explosion":
            await _handle_api(api_bom_explosion, scope, receive, add_cors)
        elif path == "/api/debug/bom":
            await _handle_api(api_bom_debug, scope, receive, add_cors)
        elif path == "/api/debug/valuation":
            await _handle_api(api_debug_valuation, scope, receive, add_cors)
        elif _ui_exists and (path == "/" or path == "" or path.startswith("/assets/") or path.endswith(".svg") or path.endswith(".ico") or path.endswith(".html")):
            static_path = "index.html" if path in ("/", "") else path
            await _serve_static(static_path, scope, receive, send)
        else:
            await a2a_app(scope, receive, send)

        # Always clear credentials after request completes
        clear_request_auth()

    app = root_asgi

    return app


application = build_app()


def main():
    logger.info("Starting at http://%s:%d", HOST, PORT)
    uvicorn.run(application, host=HOST, port=PORT)


if __name__ == "__main__":
    main()

"""
BOMA v2.0 — Layer 5: Deterministic Calculator
===============================================
Pure arithmetic — no LLM involved.
All financial calculations happen here, not inside the LLM.

Handles:
  - Savings calculation (price diff → INR savings + %)
  - Budget / units affordable (floor division)
  - Multi-step budget (buy A, remaining → buy B)
  - FX conversion
  - Price variance %
"""

from __future__ import annotations

import math
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_fx_rate(tool_results: dict, from_currency: str = "USD", to_currency: str = "INR") -> float:
    """Extract FX rate from tool result or use cached live rate."""
    try:
        from agent import get_live_fx_rates
        rates = get_live_fx_rates()
        key = f"{from_currency}_{to_currency}"
        rate = rates.get(key)
        if rate:
            return float(rate)
        # Try inverse
        rev_key = f"{to_currency}_{from_currency}"
        rev_rate = rates.get(rev_key)
        if rev_rate and float(rev_rate) > 0:
            return round(1.0 / float(rev_rate), 6)
    except Exception as e:
        logger.warning("[calculator] FX lookup failed: %s", e)
    # Hard fallback
    return 95.69 if (from_currency, to_currency) == ("USD", "INR") else 1.0


def to_inr(amount: float, currency: str, fx_rate: Optional[float] = None) -> float:
    """Convert any amount to INR."""
    if currency == "INR":
        return round(amount, 2)
    if fx_rate is None:
        fx_rate = get_fx_rate({}, currency, "INR")
    return round(amount * fx_rate, 2)


def calculate_savings(
    price1: float, currency1: str,
    price2: float, currency2: str,
    plant1: str, plant2: str,
) -> dict:
    """
    Calculate savings when purchasing from cheaper plant vs expensive plant.

    Returns:
        {
          cheaper_plant, expensive_plant,
          price_cheaper, price_expensive, currency,
          savings_original, savings_inr,
          savings_pct,
          price_cheaper_inr, price_expensive_inr,
          fx_rate, fx_source
        }
    """
    fx1 = get_fx_rate({}, currency1, "INR")
    fx2 = get_fx_rate({}, currency2, "INR")

    price1_inr = to_inr(price1, currency1, fx1)
    price2_inr = to_inr(price2, currency2, fx2)

    if price1_inr <= price2_inr:
        cheaper_plant, expensive_plant = plant1, plant2
        price_cheaper_inr, price_expensive_inr = price1_inr, price2_inr
        price_cheaper, price_expensive = price1, price2
        currency_cheaper, currency_expensive = currency1, currency2
    else:
        cheaper_plant, expensive_plant = plant2, plant1
        price_cheaper_inr, price_expensive_inr = price2_inr, price1_inr
        price_cheaper, price_expensive = price2, price1
        currency_cheaper, currency_expensive = currency2, currency1

    savings_inr = round(price_expensive_inr - price_cheaper_inr, 2)
    savings_pct = round(savings_inr / price_expensive_inr * 100, 2) if price_expensive_inr > 0 else 0.0

    logger.info(
        "[calc] savings: cheaper=%s(%.2f INR) vs expensive=%s(%.2f INR) → save ₹%.2f (%.1f%%)",
        cheaper_plant, price_cheaper_inr, expensive_plant, price_expensive_inr, savings_inr, savings_pct,
    )

    return {
        "cheaper_plant": cheaper_plant,
        "expensive_plant": expensive_plant,
        "price_cheaper": price_cheaper,
        "price_expensive": price_expensive,
        "currency_cheaper": currency_cheaper,
        "currency_expensive": currency_expensive,
        "price_cheaper_inr": price_cheaper_inr,
        "price_expensive_inr": price_expensive_inr,
        "savings_inr": savings_inr,
        "savings_pct": savings_pct,
        "fx_rate_1": fx1,
        "fx_rate_2": fx2,
    }


def calculate_budget_units(
    budget: float,
    budget_currency: str,
    unit_price: float,
    unit_currency: str,
    plant: str,
    material: str,
) -> dict:
    """
    Calculate how many units can be purchased with a given budget.

    Returns:
        {
          budget_inr, unit_price_inr,
          units_affordable, total_spent_inr, remaining_inr,
          units_affordable_floor, plant, material
        }
    """
    budget_inr = to_inr(budget, budget_currency)
    unit_price_inr = to_inr(unit_price, unit_currency)

    if unit_price_inr <= 0:
        return {
            "error": f"Unit price is zero for {material} at plant {plant}",
            "units_affordable": 0,
        }

    units = math.floor(budget_inr / unit_price_inr)
    total_spent = round(units * unit_price_inr, 2)
    remaining = round(budget_inr - total_spent, 2)

    logger.info(
        "[calc] budget: ₹%.0f / ₹%.2f per unit → %d units, spent ₹%.0f, remaining ₹%.0f",
        budget_inr, unit_price_inr, units, total_spent, remaining,
    )

    return {
        "material": material,
        "plant": plant,
        "budget_original": budget,
        "budget_currency": budget_currency,
        "budget_inr": budget_inr,
        "unit_price": unit_price,
        "unit_currency": unit_currency,
        "unit_price_inr": unit_price_inr,
        "units_affordable": units,
        "total_spent_inr": total_spent,
        "remaining_inr": remaining,
    }


def calculate_multi_step_budget(
    budget: float,
    budget_currency: str,
    # Step 1
    material1: str, plant1: str, price1: float, currency1: str,
    # Step 2
    material2: str, plant2: str, price2: float, currency2: str,
) -> dict:
    """
    Sequential budget: buy material1 from plant1, then use remaining to buy material2.
    """
    step1 = calculate_budget_units(budget, budget_currency, price1, currency1, plant1, material1)
    if step1.get("error"):
        return step1

    remaining_inr = step1["remaining_inr"]
    # Step 2 uses remaining budget (already in INR)
    step2 = calculate_budget_units(remaining_inr, "INR", price2, currency2, plant2, material2)

    return {
        "step1": step1,
        "step2": step2,
        "total_budget_inr": step1["budget_inr"],
        "summary": (
            f"With ₹{step1['budget_inr']:,.0f}: buy {step1['units_affordable']} units of {material1} "
            f"(₹{step1['total_spent_inr']:,.0f}), then with remaining ₹{remaining_inr:,.0f} "
            f"buy {step2['units_affordable']} units of {material2} (₹{step2.get('total_spent_inr',0):,.0f})."
        ),
    }


def calculate_assembly_cost(
    components: list,
    extra_charges: list,
    budget: float = 0.0,
    budget_currency: str = "USD",
) -> dict:
    """
    Total cost per assembly unit across multiple components + extra charges, then
    how many units a budget can buy.

    components:    [{name, qty, price, currency, plant}, ...]
    extra_charges: [{name, amount, currency}, ...]
    budget:        total available budget (0 = skip budget analysis)
    """
    component_rows = []
    total_inr = 0.0

    for comp in components:
        price    = float(comp.get("price", 0))
        qty      = float(comp.get("qty", 1))
        currency = comp.get("currency", "USD")
        fx       = get_fx_rate({}, currency, "INR")
        unit_inr = to_inr(price, currency, fx)
        line_inr = round(unit_inr * qty, 2)
        total_inr += line_inr
        component_rows.append({
            "name":          comp.get("name", ""),
            "plant":         comp.get("plant", ""),
            "qty":           qty,
            "unit_price":    price,
            "currency":      currency,
            "unit_price_inr": unit_inr,
            "line_total_inr": line_inr,
        })

    charge_rows = []
    for charge in extra_charges:
        amount   = float(charge.get("amount", 0))
        currency = charge.get("currency", "USD")
        fx       = get_fx_rate({}, currency, "INR")
        inr      = to_inr(amount, currency, fx)
        total_inr += inr
        charge_rows.append({
            "name":       charge.get("name", ""),
            "amount":     amount,
            "currency":   currency,
            "amount_inr": inr,
        })

    total_inr = round(total_inr, 2)

    logger.info("[calc] assembly: %d components, %d charges → ₹%.2f per unit",
                len(component_rows), len(charge_rows), total_inr)

    result: dict = {
        "components":              component_rows,
        "extra_charges":           charge_rows,
        "total_cost_per_unit_inr": total_inr,
    }

    if budget > 0 and total_inr > 0:
        budget_inr  = to_inr(budget, budget_currency)
        units       = math.floor(budget_inr / total_inr)
        spent       = round(units * total_inr, 2)
        remaining   = round(budget_inr - spent, 2)
        logger.info("[calc] assembly budget: ₹%.0f / ₹%.2f per unit → %d units",
                    budget_inr, total_inr, units)
        result.update({
            "budget":          budget,
            "budget_currency": budget_currency,
            "budget_inr":      budget_inr,
            "units_affordable": units,
            "total_spent_inr": spent,
            "remaining_inr":   remaining,
        })

    return result


def calculate_variance_pct(price1: float, price2: float) -> float:
    """Calculate absolute variance percentage between two prices."""
    if max(abs(price1), abs(price2)) == 0:
        return 0.0
    return round(abs(price1 - price2) / max(abs(price1), abs(price2)) * 100, 2)


def format_inr(amount: float) -> str:
    """Format amount as ₹ with Indian number formatting."""
    try:
        if amount >= 1e7:
            return f"₹{amount/1e7:.2f} Cr (₹{amount:,.0f})"
        elif amount >= 1e5:
            return f"₹{amount/1e5:.2f} Lakh (₹{amount:,.0f})"
        else:
            return f"₹{amount:,.2f}"
    except Exception:
        return f"₹{amount}"

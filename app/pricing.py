"""
Pricing — exact port of Dropy Central's Black Box formula
(app/api_routes/blackbox_routes.py :: calculate_blackbox_price)
plus Prizer's round-to-9 and Fibonacci MRP tiers.

Ported, not imported, so this tool runs standalone.
If you change the formula in Dropy Central, change it here too.
"""
import math
from typing import Optional

from .config import settings


# ── MRP tiers (from PRIZER_FLOW.md) ─────────────────────────────
# (max_selling_price, markup_percent)
MRP_TIERS = [
    (999, 34),
    (1999, 21),
    (3499, 21),
    (5999, 13),
    (9999, 13),
    (15999, 8),
]
MRP_TOP_TIER_MARKUP = 5  # anything above 15,999


def blackbox_price(amazon_usd: float, weight_g: float,
                   constants: Optional[dict] = None) -> float:
    """
    Raw Black Box price in INR (not yet rounded).

    1. INR Purchase  = (amazon_$ + 2%) x Dollar_Exchange
    2. Weight Charge = weight_g x Cargo_Markup
    3. Subtotal      = 1 + 2
    4. C             = Subtotal + (Tax% x Subtotal)
    5. D             = C + (Tax% x C)
    6. Final         = D + (Profit% x D)
    """
    c = constants or settings.pricing_constants

    dollar = float(c["dollar_exchange"])
    cargo = float(c["cargo_markup"])
    tax = float(c["tax_margin"]) / 100.0
    profit = float(c["profit_margin"]) / 100.0

    inr_purchase = (amazon_usd + (0.02 * amazon_usd)) * dollar
    weight_charges = weight_g * cargo
    subtotal = inr_purchase + weight_charges

    step_c = subtotal + (tax * subtotal)
    step_d = step_c + (tax * step_c)
    final = step_d + (profit * step_d)

    return round(final, 2)


def round_to_nine(price: float) -> int:
    """Round UP to the nearest number ending in 9. 2493.97 -> 2499"""
    return int(math.ceil((price + 1) / 10) * 10 - 1)


def mrp_for(selling_price: float) -> int:
    """
    Compare-at / MRP price using Fibonacci tiers, rounded up to nearest 100.
    NOTE: on Dropy this is written to the variant metafield custom.mrp (money),
    NOT to compareAtPrice.
    """
    markup = MRP_TOP_TIER_MARKUP
    for ceiling, pct in MRP_TIERS:
        if selling_price <= ceiling:
            markup = pct
            break
    return int(math.ceil(selling_price * (1 + markup / 100.0) / 100.0) * 100)


def compute(amazon_usd: float, weight_g: float,
            constants: Optional[dict] = None) -> dict:
    """
    Full pricing result for one ASIN.
    Returns {raw, price, mrp, weight_g, amazon_usd}
    """
    weight = max(float(weight_g or 0), settings.min_weight_g)
    raw = blackbox_price(float(amazon_usd), weight, constants)
    price = round_to_nine(raw)
    return {
        "amazon_usd": round(float(amazon_usd), 2),
        "weight_g": int(round(weight)),
        "raw": raw,
        "price": price,
        "mrp": mrp_for(price),
    }

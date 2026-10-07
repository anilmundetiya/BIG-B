"""
Keepa CSV parsing.

Keepa exports vary in column naming between Product Viewer versions —
notably "Buy Box: Current" vs "Buy Box \U0001f69a: Current" (with truck emoji).
Dropy Central's blackbox_routes.py reads the emoji variant. We accept both,
plus a loose fallback that matches by prefix.
"""
import io
import re
from typing import Optional, Dict, Any

import pandas as pd

# ── Column name candidates, in priority order ───────────────────
COL_ASIN = ["ASIN", "Asin", "asin"]

COL_TITLE = ["Title", "Product Title", "title"]

COL_BUYBOX = [
    "Buy Box \U0001f69a: Current",   # with truck emoji (what Dropy Central uses)
    "Buy Box: Current",
    "Buy Box Current",
]

COL_NEW = [
    "New: Current",
    "New Current",
    "Amazon: Current",
]

COL_WEIGHT = [
    "Package: Weight (g)",
    "Package Weight (g)",
    "Item: Weight (g)",
]

COL_STOCK = [
    "Buy Box: Availability of the Buy Box",
    "Buy Box Availability of the Buy Box",
    "Availability of the Buy Box",
]

COL_UPC = ["Product Codes: UPC", "Product Codes UPC", "UPC"]
COL_EAN = ["Product Codes: EAN", "Product Codes EAN", "EAN"]
COL_GTIN = ["Product Codes: GTIN", "Product Codes GTIN", "GTIN"]

_DIGITS_RE = re.compile(r"^\d+$")
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


class KeepaError(Exception):
    pass


def _pick_col(df: pd.DataFrame, candidates: list) -> Optional[str]:
    """Exact match first, then case-insensitive, then prefix match."""
    for c in candidates:
        if c in df.columns:
            return c
    lower = {str(c).strip().lower(): c for c in df.columns}
    for c in candidates:
        hit = lower.get(c.strip().lower())
        if hit:
            return hit
    # Loose: strip emoji/punctuation and compare
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(s).lower())
    normed = {_norm(c): c for c in df.columns}
    for c in candidates:
        hit = normed.get(_norm(c))
        if hit:
            return hit
    return None


def _money(value) -> Optional[float]:
    """'$16.99' / '16.99' / 1699 -> 16.99 ; junk / '-' / blank -> None"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if not s or s in ("-", "", "nan", "None"):
        return None
    s = s.replace("$", "").replace(",", "").replace("\u20b9", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def resolve_barcode(row: dict) -> Optional[str]:
    """
    UPC > EAN > GTIN. Returns a STRING (preserves leading zeros).
    Skips blanks, '-', '0', comma-separated multi-values, non-digits.
    Mirrors blackbox_routes.py :: resolve_keepa_barcode().
    """
    for key in ("_upc", "_ean", "_gtin"):
        v = row.get(key)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        s = str(v).strip()
        if not s or s in ("-", "0", "nan"):
            continue
        if "," in s:          # multi-barcode field is ambiguous
            continue
        if s.endswith(".0"):  # pandas turned it into a float
            s = s[:-2]
        if not _DIGITS_RE.match(s):
            continue
        return s
    return None


def load_keepa_csv(content: bytes) -> Dict[str, Dict[str, Any]]:
    """
    Parse a Keepa CSV into {ASIN: {price_usd, weight_g, barcode, in_stock, title}}.

    Everything is read as a string first so leading zeros in barcodes survive.
    """
    try:
        df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
    except Exception as exc:
        raise KeepaError(f"Could not read CSV: {exc}") from exc

    if df.empty:
        raise KeepaError("Keepa CSV has no rows")

    c_asin = _pick_col(df, COL_ASIN)
    if not c_asin:
        raise KeepaError(
            f"No ASIN column found. Columns present: {list(df.columns)[:12]}"
        )

    c_title = _pick_col(df, COL_TITLE)
    c_buybox = _pick_col(df, COL_BUYBOX)
    c_new = _pick_col(df, COL_NEW)
    c_weight = _pick_col(df, COL_WEIGHT)
    c_stock = _pick_col(df, COL_STOCK)
    c_upc = _pick_col(df, COL_UPC)
    c_ean = _pick_col(df, COL_EAN)
    c_gtin = _pick_col(df, COL_GTIN)

    if not c_buybox and not c_new:
        raise KeepaError(
            "No price column found (looked for Buy Box: Current / New: Current)"
        )

    out: Dict[str, Dict[str, Any]] = {}

    for _, r in df.iterrows():
        asin = str(r.get(c_asin, "")).strip().upper()
        if not asin or not _ASIN_RE.match(asin):
            continue

        price = _money(r.get(c_buybox)) if c_buybox else None
        source = "Buy Box"
        if price is None and c_new:
            price = _money(r.get(c_new))
            source = "New"
        if price is None:
            source = None

        weight = None
        if c_weight:
            w = _money(r.get(c_weight))
            weight = float(w) if w else None

        stock_text = str(r.get(c_stock, "")).strip() if c_stock else ""
        # Keepa exports "IN_STOCK" (all caps, underscore). Older/other exports
        # use "In Stock" or "1 left in stock". Normalise all three.
        low = stock_text.lower().replace("_", " ")
        in_stock = bool(stock_text) and (
            "in stock" in low or "left in stock" in low
        )
        # If there's no stock column at all, don't punish the row — a price implies stock
        if not c_stock:
            in_stock = price is not None

        barcode = resolve_barcode({
            "_upc": r.get(c_upc) if c_upc else None,
            "_ean": r.get(c_ean) if c_ean else None,
            "_gtin": r.get(c_gtin) if c_gtin else None,
        })

        out[asin] = {
            "asin": asin,
            "title": str(r.get(c_title, "")).strip() if c_title else "",
            "price_usd": price,
            "price_source": source,
            "weight_g": weight,
            "in_stock": in_stock,
            "stock_text": stock_text,
            "barcode": barcode,
        }

    if not out:
        raise KeepaError("No valid ASIN rows found in the Keepa CSV")

    return out


def columns_report(content: bytes) -> dict:
    """Debug helper — what did we match in this file?"""
    df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False, nrows=5)
    return {
        "columns_in_file": list(df.columns),
        "matched": {
            "asin": _pick_col(df, COL_ASIN),
            "title": _pick_col(df, COL_TITLE),
            "buybox": _pick_col(df, COL_BUYBOX),
            "new": _pick_col(df, COL_NEW),
            "weight": _pick_col(df, COL_WEIGHT),
            "stock": _pick_col(df, COL_STOCK),
            "upc": _pick_col(df, COL_UPC),
            "ean": _pick_col(df, COL_EAN),
            "gtin": _pick_col(df, COL_GTIN),
        },
    }

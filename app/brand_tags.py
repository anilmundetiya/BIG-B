"""
BIG-B — brand -> tag logic + ASIN/Brand CSV parsing.

brand_to_tag() is a VERBATIM copy of Dropy Central:
    app/api_routes/tag_tasks_routes.py :: brand_to_tag
    app/api_routes/ultimatium_routes.py :: _brand_to_tag

Do NOT "improve" it (e.g. accent handling). Tags must match exactly what
Dropy Central creates, or the two tools will split one brand into two tags.
Known Dropy Central behaviour, kept on purpose:
    "L'Oréal"  -> "loral"      (accented letters are dropped, not converted)
    "A&D"/"AD" -> "ad"         (both merge into one brand)

CSV parsing mirrors Ultimatium's CSV branch (process_ultimatium_upload):
  - header names matched case-insensitively ("ASIN", "Brand")
  - blank brand -> "Unknown"
  - rows whose ASIN starts with asin/sku/#/http are ignored
  - ASIN cut to its first 10 characters
  - duplicate ASINs: first row wins
  - grouped by sanitized tag; first spelling seen becomes the collection name
"""
import csv
import io
import re
from typing import Dict, List, Tuple

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def brand_to_tag(brand_name: str) -> str:
    """Fixodent -> fixodent, L'Oreal -> loreal, Mama Bear -> mamabear"""
    return re.sub(r'[^a-z0-9]', '', brand_name.lower())


class BulkFileError(Exception):
    pass


def _decode(content: bytes) -> str:
    # Dropy Central decodes utf-8-sig. Excel on Windows often saves CSV as
    # cp1252 instead, which would crash on any accented brand — fall back.
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def parse_asin_brand_csv(content: bytes) -> Tuple[List[Tuple[str, str]], List[dict]]:
    """
    Returns (pairs, rejected)
      pairs    = [(ASIN, Brand), ...] unique ASINs, file order
      rejected = [{"asin", "brand", "reason"}, ...]
    """
    if not content:
        raise BulkFileError("ASIN/Brand CSV is empty")

    reader = csv.DictReader(io.StringIO(_decode(content)))
    fieldnames = reader.fieldnames or []
    asin_col = next((f for f in fieldnames if f and f.strip().upper() == "ASIN"), None)
    brand_col = next((f for f in fieldnames if f and f.strip().upper() == "BRAND"), None)

    if not asin_col:
        raise BulkFileError(f"CSV must have an 'ASIN' column. Found: {fieldnames[:8]}")
    if not brand_col:
        raise BulkFileError(f"CSV must have a 'Brand' column. Found: {fieldnames[:8]}")

    pairs: List[Tuple[str, str]] = []
    rejected: List[dict] = []
    seen = set()

    for row in reader:
        asin = (row.get(asin_col) or "").strip().strip('"').strip("'")
        brand = (row.get(brand_col) or "Unknown").strip().strip('"').strip("'")
        if not brand:
            brand = "Unknown"

        # Ultimatium's row filter
        if not asin or len(asin) < 10 or asin.lower().startswith(("asin", "sku", "#", "http")):
            if asin:
                rejected.append({"asin": asin, "brand": brand, "reason": "not an ASIN"})
            continue

        asin = asin[:10].upper()
        if not _ASIN_RE.match(asin):
            rejected.append({"asin": asin, "brand": brand, "reason": "invalid ASIN format"})
            continue
        if asin in seen:
            continue
        seen.add(asin)
        pairs.append((asin, brand))

    if not pairs:
        raise BulkFileError("No valid ASINs found in the CSV")
    return pairs, rejected


def group_by_brand(pairs: List[Tuple[str, str]]) -> Tuple[List[dict], List[dict]]:
    """
    Group by sanitized tag (merges "La Roche-Posay" + "LA ROCHE-POSAY").
    Returns (groups, rejected)
      groups   = [{"brand": display_name, "tag": tag, "asins": [...]}, ...] file order
      rejected = ASINs whose brand produces an EMPTY tag (e.g. non-Latin brand)
    """
    tag_to_group: Dict[str, dict] = {}
    order: List[str] = []
    rejected: List[dict] = []

    for asin, brand in pairs:
        tag = brand_to_tag(brand)
        if not tag:
            rejected.append({"asin": asin, "brand": brand,
                             "reason": "brand gives an empty tag (no a-z/0-9 characters)"})
            continue
        if tag not in tag_to_group:
            tag_to_group[tag] = {"brand": brand, "tag": tag, "asins": []}
            order.append(tag)
        tag_to_group[tag]["asins"].append(asin)

    return [tag_to_group[t] for t in order], rejected

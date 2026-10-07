"""
Mr Scrape client (HTTP -> localhost:5003).

Mr Scrape's exact /scrape request shape was not available when this was written,
so this client AUTO-DETECTS it: on the first scrape it tries a list of candidate
(method, path, payload) shapes until one returns a usable product, then caches
the winner for the rest of the session.

If you later confirm the real contract, delete the candidates you don't need and
put the correct one first — nothing else in this tool changes.

Hit  GET /api/probe-scraper  in this tool to see which shape matched.
"""
import asyncio
import re
from typing import Any, Dict, List, Optional

import httpx

from .config import settings

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

# Candidate request shapes, tried in order.
# {"method", "path", "kind": json|form|query}
CANDIDATES = [
    {"method": "POST", "path": "/scrape", "kind": "json"},
    {"method": "POST", "path": "/scrape", "kind": "form"},
    {"method": "GET", "path": "/scrape", "kind": "query"},
    {"method": "POST", "path": "/api/scrape", "kind": "json"},
    {"method": "POST", "path": "/scrape-product", "kind": "json"},
    {"method": "GET", "path": "/scrape-product", "kind": "query"},
]

# Field-name aliases accepted for the ASIN/url parameter
ASIN_KEYS = ["asin", "url", "asins", "input", "product_url"]
TAG_KEYS = ["tags", "tag", "brand"]


class ScrapeError(Exception):
    pass


class MrScrapeClient:
    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or settings.mr_scrape_url).rstrip("/")
        self._shape: Optional[dict] = None   # cached winning candidate
        self._asin_key: Optional[str] = None
        self._lock = asyncio.Lock()

    # ── Health ──────────────────────────────────────────────────
    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(self.base_url + "/")
                return r.status_code < 500
        except Exception:
            return False

    # ── Request builders ────────────────────────────────────────
    def _build(self, shape: dict, asin_key: str, asin: str, tags: str) -> dict:
        url = self.base_url + shape["path"]
        payload = {asin_key: asin}
        for tk in TAG_KEYS:
            payload[tk] = tags
        kind = shape["kind"]
        if kind == "json":
            return {"method": shape["method"], "url": url, "json": payload}
        if kind == "form":
            return {"method": shape["method"], "url": url, "data": payload}
        return {"method": shape["method"], "url": url, "params": payload}

    async def _try(self, client: httpx.AsyncClient, req: dict) -> Optional[dict]:
        try:
            r = await client.request(**req)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None
        return _normalize(data)

    # ── Public API ──────────────────────────────────────────────
    async def scrape_one(self, asin: str, tags: str = "") -> dict:
        """Scrape one ASIN. Raises ScrapeError on failure."""
        asin = asin.strip().upper()
        if not _ASIN_RE.match(asin):
            raise ScrapeError(f"'{asin}' is not a valid 10-character ASIN")

        timeout = httpx.Timeout(settings.scrape_timeout, connect=10.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            # Fast path — we already know the shape
            if self._shape and self._asin_key:
                req = self._build(self._shape, self._asin_key, asin, tags)
                product = await self._try(client, req)
                if product:
                    product["asin"] = asin
                    return product
                # Cached shape stopped working — fall through and re-detect
                async with self._lock:
                    self._shape = None
                    self._asin_key = None

            # Detection path
            async with self._lock:
                for shape in CANDIDATES:
                    for asin_key in ASIN_KEYS:
                        req = self._build(shape, asin_key, asin, tags)
                        product = await self._try(client, req)
                        if product:
                            self._shape = shape
                            self._asin_key = asin_key
                            product["asin"] = asin
                            return product

        raise ScrapeError(
            f"Mr Scrape at {self.base_url} did not return usable product data for {asin}. "
            f"Tried {len(CANDIDATES) * len(ASIN_KEYS)} request shapes. "
            f"Check Mr Scrape is running, then see GET /api/probe-scraper."
        )

    async def probe(self) -> dict:
        """Report what Mr Scrape exposes — for debugging the contract."""
        report: Dict[str, Any] = {"base_url": self.base_url, "reachable": False,
                                  "detected_shape": self._shape,
                                  "detected_asin_key": self._asin_key,
                                  "endpoints": []}
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                root = await c.get(self.base_url + "/")
                report["reachable"] = root.status_code < 500
                report["root_status"] = root.status_code
                for path in ["/scrape", "/api/scrape", "/scrape-product",
                             "/read-input-file", "/download-shopify-csv"]:
                    try:
                        r = await c.get(self.base_url + path)
                        report["endpoints"].append({"path": path, "GET": r.status_code})
                    except Exception as e:
                        report["endpoints"].append({"path": path, "GET": f"err: {e}"})
        except Exception as e:
            report["error"] = str(e)
        return report


# ── Response normalisation ──────────────────────────────────────

def _first(d: dict, keys: List[str], default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    return default


def _normalize(raw: Any) -> Optional[dict]:
    """
    Turn whatever Mr Scrape returns into our canonical product dict.
    Returns None if it clearly isn't a product (so detection keeps trying).

    Canonical shape:
      {title, vendor, product_type, body_html, images[], weight_g,
       price, handle, variants[], seo_title, seo_description}
    """
    if raw is None:
        return None

    # Unwrap common envelopes: {"success":true,"product":{...}} / {"data":{...}}
    if isinstance(raw, dict):
        for key in ("product", "data", "result", "results"):
            inner = raw.get(key)
            if isinstance(inner, dict):
                raw = inner
                break
            if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                raw = inner[0]
                break
    if isinstance(raw, list):
        if not raw or not isinstance(raw[0], dict):
            return None
        raw = raw[0]
    if not isinstance(raw, dict):
        return None

    # An explicit failure envelope is not a product
    if raw.get("success") is False or raw.get("error"):
        return None

    title = _first(raw, ["title", "Title", "product_title", "name"])
    if not title or not str(title).strip():
        return None  # no title => not a usable product

    images = _first(raw, ["images", "Images", "image_urls", "image_src"], []) or []
    if isinstance(images, str):
        images = [i.strip() for i in images.split(",") if i.strip()]
    images = [str(i).strip() for i in images if str(i).strip().startswith("http")]

    weight = _first(raw, ["weight_grams", "weight_g", "weight", "grams"], 0)
    try:
        weight_g = float(weight or 0)
    except (TypeError, ValueError):
        weight_g = 0.0

    price = _first(raw, ["price", "Price", "variant_price"], 0)
    try:
        price_val = float(str(price).replace("$", "").replace(",", "") or 0)
    except (TypeError, ValueError):
        price_val = 0.0

    variants_raw = _first(raw, ["variants", "Variants"], []) or []
    variants = []
    if isinstance(variants_raw, list):
        for v in variants_raw:
            if not isinstance(v, dict):
                continue
            v_asin = str(_first(v, ["asin", "ASIN", "variant_asin"], "") or "").upper()
            if not _ASIN_RE.match(v_asin):
                continue
            variants.append({
                "asin": v_asin,
                "option_name": str(_first(v, ["option_name", "dimension", "name"], "Title")),
                "option_value": str(_first(v, ["option_value", "display_value", "value"], "")),
                "price": _first(v, ["price"], None),
                "image": _first(v, ["image", "image_url"], None),
            })

    return {
        "title": str(title).strip(),
        "vendor": str(_first(raw, ["vendor", "Vendor", "brand", "Brand"], "") or "").strip(),
        "product_type": str(_first(raw, ["product_type", "productType", "category", "Product Category"], "") or "").strip(),
        "body_html": str(_first(raw, ["body_html", "description", "Body (HTML)", "descriptionHtml"], "") or ""),
        "images": images,
        "weight_g": weight_g,
        "price": price_val,
        "handle": str(_first(raw, ["handle", "Handle"], "") or "").strip(),
        "variants": variants,
        "seo_title": str(_first(raw, ["seo_title", "SEO Title"], "") or "").strip(),
        "seo_description": str(_first(raw, ["seo_description", "SEO Description"], "") or "").strip(),
        "rating": _first(raw, ["rating"], None),
        "_raw_keys": sorted(raw.keys())[:40],
    }


def fallback_handle(title: str, asin: str) -> str:
    """Mirror Mr Scrape's handle rule: slug(title)[:80] + '-' + asin.lower()"""
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    slug = slug[:80].rstrip("-")
    return f"{slug}-{asin.lower()}" if slug else asin.lower()


mr_scrape = MrScrapeClient()

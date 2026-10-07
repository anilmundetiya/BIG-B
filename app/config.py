"""
Configuration — reads .env

The .env file is searched for in this order:
  1. TEN_ASIN_ENV environment variable (explicit full path to a .env file)
  2. ten-asin/.env            (inside this folder)
  3. ../.env                  (parent folder)
  4. ../../.env               (grandparent folder)

That way you can drop this folder next to an existing .env and it just works.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent          # .../ten-asin/app
ROOT = HERE.parent                              # .../ten-asin


def _find_env() -> Path:
    explicit = os.environ.get("TEN_ASIN_ENV")
    if explicit and Path(explicit).is_file():
        return Path(explicit).resolve()

    for candidate in (ROOT / ".env", ROOT.parent / ".env", ROOT.parent.parent / ".env"):
        if candidate.is_file():
            return candidate.resolve()

    # Nothing found — return the in-folder path so the error message is useful
    return (ROOT / ".env").resolve()


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        self.env_path = _find_env()
        load_dotenv(dotenv_path=self.env_path, override=False)

        # ── Shopify (required) ──
        self.shop_domain = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").strip()
        self.access_token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "").strip()
        self.api_version = os.environ.get("SHOPIFY_API_VERSION", "2025-10").strip()

        # ── Mr Scrape (optional override) ──
        self.mr_scrape_url = os.environ.get(
            "MR_SCRAPE_URL", "http://localhost:5003"
        ).strip().rstrip("/")

        # ── Pricing constants (optional overrides) ──
        # Defaults match Dropy Central's blackbox_routes.py get_user_constants()
        self.dollar_exchange = _f("DOLLAR_EXCHANGE", 106.0)
        self.cargo_markup = _f("CARGO_MARKUP", 1.1)
        self.tax_margin = _f("TAX_MARGIN", 3.0)
        self.profit_margin = _f("PROFIT_MARGIN", 30.0)

        # ── Behaviour knobs ──
        self.min_weight_g = _f("MIN_WEIGHT_G", 80.0)
        self.sku_prefix = os.environ.get("SKU_PREFIX", "Dropy").strip()
        self.max_asins = int(_f("MAX_ASINS", 10))
        # BIG-B runs on its own port so it can sit next to ten-asin (1999).
        # Uses BIGB_PORT, NOT TEN_ASIN_PORT, so a copied ten-asin .env cannot clash.
        self.port = int(_f("BIGB_PORT", 2000))
        self.scrape_timeout = _f("SCRAPE_TIMEOUT", 180.0)

        # ── Phase 6 variant scraper ──
        # visible Chrome window per JD's choice; page wait mirrors Mr Scrape's
        self.variant_visible = os.environ.get(
            "VARIANT_VISIBLE", "true").strip().lower() not in ("0", "false", "no")
        self.variant_page_wait = _f("VARIANT_PAGE_WAIT", 4.0)

    # ── Helpers ──
    @property
    def shopify_graphql_url(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"

    @property
    def admin_base_url(self) -> str:
        return f"https://{self.shop_domain}/admin"

    @property
    def pricing_constants(self) -> dict:
        return {
            "dollar_exchange": self.dollar_exchange,
            "cargo_markup": self.cargo_markup,
            "tax_margin": self.tax_margin,
            "profit_margin": self.profit_margin,
        }

    def missing_keys(self) -> list:
        missing = []
        if not self.shop_domain:
            missing.append("SHOPIFY_SHOP_DOMAIN")
        if not self.access_token:
            missing.append("SHOPIFY_ACCESS_TOKEN")
        if not self.api_version:
            missing.append("SHOPIFY_API_VERSION")
        return missing


settings = Settings()

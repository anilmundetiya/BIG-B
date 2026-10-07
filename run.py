"""
BIG-B — entrypoint
Run:  python run.py
Then: http://localhost:2000
"""
import sys
import uvicorn

from app.config import settings


def preflight() -> bool:
    """Fail loudly at startup if .env is not configured."""
    missing = settings.missing_keys()
    if missing:
        print("=" * 60)
        print("  CONFIG ERROR — cannot start")
        print("=" * 60)
        print(f"  .env file looked for at: {settings.env_path}")
        print("  Missing required keys:")
        for k in missing:
            print(f"    - {k}")
        print()
        print("  Add them to your .env file, then run again:")
        print("    SHOPIFY_SHOP_DOMAIN=your-store.myshopify.com")
        print("    SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxxxx")
        print("    SHOPIFY_API_VERSION=2025-10")
        print("=" * 60)
        return False

    print("=" * 60)
    print("  BIG-B")
    print("=" * 60)
    print(f"  .env         : {settings.env_path}")
    print(f"  Shop         : {settings.shop_domain}")
    print(f"  API version  : {settings.api_version}")
    print(f"  Mr Scrape    : {settings.mr_scrape_url}")
    print(f"  Pricing      : $1 = Rs {settings.dollar_exchange} | "
          f"cargo {settings.cargo_markup}/g | tax {settings.tax_margin}% x2 | "
          f"profit {settings.profit_margin}%")
    print(f"  URL          : http://localhost:{settings.port}")
    print("=" * 60)
    return True


if __name__ == "__main__":
    if not preflight():
        sys.exit(1)
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=settings.port,
        reload=False,
        log_level="info",
    )

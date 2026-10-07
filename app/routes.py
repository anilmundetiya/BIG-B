"""API routes."""
import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import keepa as keepa_mod
from .config import settings
from .flow import parse_asins, run_job
from .jobs import store
from .scraper import mr_scrape
from .shopify import ShopifyError, shopify

router = APIRouter()

# Absolute path — works no matter which directory the server was started from
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Starlette >= 0.29 wants request first. Fall back for older versions.
    context = {
        "shop": settings.shop_domain,
        "max_asins": settings.max_asins,
        "mr_scrape": settings.mr_scrape_url,
        "constants": settings.pricing_constants,
    }
    try:
        return templates.TemplateResponse(request, "index.html", context)
    except TypeError:
        return templates.TemplateResponse("index.html", {"request": request, **context})


@router.get("/api/probe-variant/{asin}")
async def probe_variant(asin: str):
    """
    DEBUG: scrape ONE ASIN's variants from Amazon and return what was found.
    Opens a visible Chrome. Use this if Phase 6 finds 0 variants on a product
    you know has them. Example: http://localhost:1999/api/probe-variant/B085L33PPB
    """
    import asyncio as _asyncio
    from .variant_scraper import VariantScraper, VariantScrapeError

    def _run():
        s = VariantScraper(visible=settings.variant_visible,
                           page_wait=settings.variant_page_wait)
        try:
            data = s.scrape(asin.strip().upper())
            return {"ok": True, "asin": asin.strip().upper(),
                    "title": data.get("title"),
                    "captcha": data.get("captcha"),
                    "variant_count": len(data.get("variants") or []),
                    "variants": data.get("variants") or []}
        except VariantScrapeError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            s.close()

    # run the blocking Selenium call off the event loop
    return await _asyncio.to_thread(_run)


@router.get("/api/probe-sibling-write/{pid}")
async def probe_sibling_write(pid: str):
    """
    DEBUG: write each sibling metafield to a real product ONE AT A TIME,
    so we see exactly which key Shopify rejects and why.
    pid = numeric Shopify product id (from a product's admin URL).
    Example: http://localhost:1999/api/probe-sibling-write/7435821908048
    """
    from . import siblings as sib
    gid = f"gid://shopify/Product/{pid}"
    all_gids = [gid, "gid://shopify/Product/0000000000"]  # dummy 2-item list
    mf = sib.build_metafields_for_member(pid, all_gids, "Test Label",
                                         "Test Group", "1470000")
    results = []
    for one in mf:
        try:
            errs = await shopify.set_metafields([one])
            results.append({
                "key": one["key"], "type": one["type"],
                "value_sent": one["value"][:80],
                "ok": not errs,
                "errors": errs,
            })
        except Exception as exc:  # noqa: BLE001
            results.append({"key": one["key"], "ok": False, "exception": str(exc)})
    return {"pid": pid, "results": results}


@router.get("/api/health")
async def health():
    out = {
        "shop_domain": settings.shop_domain,
        "api_version": settings.api_version,
        "mr_scrape_url": settings.mr_scrape_url,
        "env_path": str(settings.env_path),
        "constants": settings.pricing_constants,
    }
    try:
        shop = await shopify.ping()
        out["shopify"] = {"ok": True, "name": shop.get("name"),
                          "currency": shop.get("currencyCode")}
    except Exception as exc:  # noqa: BLE001
        out["shopify"] = {"ok": False, "error": str(exc)[:200]}

    out["mr_scrape"] = {"ok": await mr_scrape.ping()}
    return out


@router.get("/api/probe-scraper")
async def probe_scraper():
    """What does Mr Scrape actually expose? Use this if Phase 3 fails."""
    return await mr_scrape.probe()


@router.post("/api/inspect-keepa")
async def inspect_keepa(keepa_csv: UploadFile = File(...)):
    """Preview which Keepa columns matched — useful when a CSV looks odd."""
    content = await keepa_csv.read()
    try:
        report = keepa_mod.columns_report(content)
        data = keepa_mod.load_keepa_csv(content)
        report["rows_parsed"] = len(data)
        report["sample"] = list(data.values())[:3]
        return report
    except keepa_mod.KeepaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/run")
async def start_run(
    asins: str = Form(""),
    collection: str = Form(...),
    tag: str = Form(...),
    keepa_csv: UploadFile = File(...),
    asin_file: Optional[UploadFile] = File(None),
    variant_mode: str = Form("false"),
    seo_mode: str = Form("false"),
    price_overrides: str = Form(""),
):
    raw = asins or ""
    if asin_file is not None and asin_file.filename:
        try:
            raw = raw + "\n" + (await asin_file.read()).decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400,
                                detail=f"Could not read ASIN file: {exc}") from exc

    asin_list = parse_asins(raw)
    if not asin_list:
        raise HTTPException(status_code=400, detail="No ASINs provided")
    if len(asin_list) > settings.max_asins:
        raise HTTPException(
            status_code=400,
            detail=f"{len(asin_list)} ASINs given, max is {settings.max_asins}",
        )
    if not collection.strip():
        raise HTTPException(status_code=400, detail="Collection name is required")
    if not tag.strip():
        raise HTTPException(status_code=400, detail="Tag is required")

    keepa_bytes = await keepa_csv.read()
    if not keepa_bytes:
        raise HTTPException(status_code=400, detail="Keepa CSV is empty")

    vmode = str(variant_mode).strip().lower() in ("1", "true", "on", "yes")
    smode = str(seo_mode).strip().lower() in ("1", "true", "on", "yes")

    # Optional per-ASIN USD price overrides (replace Keepa price before formula)
    overrides = {}
    if price_overrides and price_overrides.strip():
        try:
            raw_ov = json.loads(price_overrides)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400,
                                detail=f"Invalid price_overrides: {exc}") from exc
        if isinstance(raw_ov, dict):
            in_run = set(asin_list)
            for a, v in raw_ov.items():
                au = str(a).strip().upper()
                if au not in in_run:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if fv > 0:
                    overrides[au] = round(fv, 2)

    job = store.create(asin_list, collection.strip(), tag.strip(),
                       variant_mode=vmode, seo_mode=smode,
                       price_overrides=overrides)
    asyncio.create_task(run_job(job, keepa_bytes))
    return JSONResponse({"job_id": job.id, "asins": asin_list,
                         "variant_mode": vmode, "seo_mode": smode,
                         "price_overrides": overrides})


@router.get("/api/status/{job_id}")
async def status(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.snapshot()


@router.get("/api/jobs")
async def jobs():
    return {"jobs": store.recent()}


@router.get("/api/stream/{job_id}")
async def stream(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def gen():
        q = job.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    if job.status in ("done", "failed"):
                        break
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "failed"):
                    break
        finally:
            job.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

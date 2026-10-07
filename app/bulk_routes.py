"""
BIG-B routes. Registered BEFORE routes.router in main.py, so "/" shows the
bulk page. The original single-run page is still available at /single.
routes.py itself is unchanged.
"""
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import bulk
from . import keepa as keepa_mod
from .brand_tags import BulkFileError
from .config import settings
from .jobs import store
from .routes import templates
from .shopify import ShopifyError

router = APIRouter()


def _ctx() -> dict:
    return {
        "shop": settings.shop_domain,
        "max_asins": settings.max_asins,
        "mr_scrape": settings.mr_scrape_url,
        "constants": settings.pricing_constants,
        "port": settings.port,
    }


def _render(request: Request, name: str):
    ctx = _ctx()
    try:
        return templates.TemplateResponse(request, name, ctx)
    except TypeError:
        return templates.TemplateResponse(name, {"request": request, **ctx})


@router.get("/", response_class=HTMLResponse)
async def bulk_page(request: Request):
    return _render(request, "bulk.html")


@router.get("/single", response_class=HTMLResponse)
async def single_page(request: Request):
    return _render(request, "index.html")


def _flag(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "on", "yes")


def _busy_reason() -> Optional[str]:
    rid = bulk.any_active()
    if rid:
        return f"BIG-B run {rid} is still running - stop it or wait for it to finish"
    for j in store.recent(20):
        if j.get("status") == "running":
            return f"A single run ({j.get('job_id')}) is in progress on /single - wait for it"
    return None


@router.post("/api/bulk/preview")
async def bulk_preview(asin_csv: UploadFile = File(...),
                       keepa_csv: Optional[UploadFile] = File(None)):
    csv_bytes = await asin_csv.read()
    keepa_bytes = await keepa_csv.read() if (keepa_csv and keepa_csv.filename) else None
    try:
        return await bulk.preview(csv_bytes, keepa_bytes)
    except (BulkFileError, keepa_mod.KeepaError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ShopifyError as exc:
        raise HTTPException(status_code=502, detail=f"Shopify: {exc}") from exc


@router.post("/api/bulk/start")
async def bulk_start(asin_csv: UploadFile = File(...),
                     keepa_csv: UploadFile = File(...),
                     seo_mode: str = Form("false"),
                     variant_mode: str = Form("false")):
    busy = _busy_reason()
    if busy:
        raise HTTPException(status_code=409, detail=busy)
    if not (asin_csv.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="ASIN/Brand file must be a .csv")
    csv_bytes = await asin_csv.read()
    keepa_bytes = await keepa_csv.read()
    if not csv_bytes:
        raise HTTPException(status_code=400, detail="ASIN/Brand CSV is empty")
    if not keepa_bytes:
        raise HTTPException(status_code=400, detail="Keepa CSV is empty")
    run = bulk.start(csv_bytes, keepa_bytes, asin_csv.filename, keepa_csv.filename,
                     _flag(seo_mode), _flag(variant_mode))
    return JSONResponse({"run_id": run.id})


@router.get("/api/bulk/runs")
async def bulk_runs():
    return {"runs": bulk.list_runs(), "active": bulk.any_active()}


@router.get("/api/bulk/status/{run_id}")
async def bulk_status(run_id: str):
    run = bulk.BulkRun.load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    out = run.public()
    job = store.get(run.current_job_id) if run.current_job_id else None
    out["current_job"] = job.snapshot() if job else None
    return out


@router.post("/api/bulk/stop/{run_id}")
async def bulk_stop(run_id: str):
    run = bulk.BulkRun.load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    bulk.request_stop(run)
    return {"ok": True, "status": run.effective_status()}


@router.post("/api/bulk/resume/{run_id}")
async def bulk_resume(run_id: str):
    run = bulk.BulkRun.load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    busy = _busy_reason()
    if busy:
        raise HTTPException(status_code=409, detail=busy)
    if not (run.folder / "input.csv").is_file() or not (run.folder / "keepa.csv").is_file():
        raise HTTPException(status_code=400, detail="Saved input files missing - cannot resume")
    bulk.resume(run)
    return {"ok": True, "run_id": run.id}


@router.get("/api/bulk/report/{run_id}")
async def bulk_report(run_id: str):
    run = bulk.BulkRun.load(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return PlainTextResponse(
        bulk.report_csv(run), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="BIG-B-{run.id}.csv"'})

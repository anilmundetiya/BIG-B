"""
BIG-B bulk runner.

Wraps the EXISTING ten-asin pipeline — it does not change it.
For every brand in the uploaded ASIN/Brand CSV:

  1. Resolve the collection + tag (Dropy Central behaviour)
       a. a collection whose TAG rule equals the brand tag  -> reuse it, use ITS rule tag
       b. else a collection whose title equals the brand    -> reuse it, use ITS rule tag
            (if it has no TAG rule = manual collection       -> brand skipped, like Dropy Central)
       c. else nothing exists                                -> new collection, title = brand,
                                                                tag = brand_to_tag(brand)
                                                                (created by the normal Phase 2)
  2. Split the brand's ASINs into chunks of MAX_ASINS (default 10)
  3. Run each chunk through flow.run_job() — the same 8-phase pipeline as ten-asin

Progress is written to bulk_runs/<run_id>/run.json after every chunk, so a crash
or restart can be resumed. Re-running a chunk is safe: upload is upsert-by-handle.
"""
import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import keepa as keepa_mod
from .brand_tags import group_by_brand, parse_asin_brand_csv
from .config import ROOT, settings
from .flow import run_job
from .jobs import store
from .shopify import ShopifyError, shopify

BULK_DIR = ROOT / "bulk_runs"
BULK_DIR.mkdir(exist_ok=True)

_RUN_LOCK = asyncio.Lock()
_TASKS: Dict[str, asyncio.Task] = {}      # keep references so tasks aren't GC'd
_RUNS: Dict[str, "BulkRun"] = {}          # in-memory cache of loaded runs

ACTIVE_STATES = ("resolving", "running", "stopping")


# ────────────────────────────────────────────────────────────────
# Shopify collection index (read-only)
# ────────────────────────────────────────────────────────────────
_COLLECTIONS_Q = """
query Cols($cursor: String) {
  collections(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id title
      ruleSet { rules { column relation condition } }
    } }
  }
}
"""


async def _gql_retry(query: str, variables: dict, tries: int = 5) -> dict:
    delay = 2.0
    for attempt in range(tries):
        try:
            return await shopify.graphql(query, variables)
        except ShopifyError as exc:
            if "THROTTLED" in str(exc).upper() and attempt < tries - 1:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise
    return {}


async def load_collection_index() -> Tuple[Dict[str, dict], Dict[str, dict], int]:
    """
    Returns (by_tag, by_title, total)
      by_tag[lower(tag_condition)] = {id, title, tags}
      by_title[lower(title)]       = {id, title, tags}
    tags = conditions of rules where column == TAG and relation == EQUALS
    """
    by_tag: Dict[str, dict] = {}
    by_title: Dict[str, dict] = {}
    cursor: Optional[str] = None
    total = 0
    while True:
        data = await _gql_retry(_COLLECTIONS_Q, {"cursor": cursor})
        conn = data.get("collections") or {}
        for e in conn.get("edges") or []:
            n = e.get("node") or {}
            rules = ((n.get("ruleSet") or {}).get("rules")) or []
            tags = [str(r.get("condition") or "").strip() for r in rules
                    if str(r.get("column")).upper() == "TAG"
                    and str(r.get("relation")).upper() == "EQUALS"
                    and str(r.get("condition") or "").strip()]
            entry = {"id": n.get("id"), "title": str(n.get("title") or "").strip(), "tags": tags}
            total += 1
            by_title.setdefault(entry["title"].lower(), entry)
            for t in tags:
                by_tag.setdefault(t.lower(), entry)
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return by_tag, by_title, total


def resolve_brand(group: dict, by_tag: Dict[str, dict], by_title: Dict[str, dict]) -> dict:
    """Decide collection + product tag for one brand group (no Shopify writes)."""
    brand, tag = group["brand"], group["tag"]

    hit = by_tag.get(tag)
    if hit:
        rule_tag = next((t for t in hit["tags"] if t.lower() == tag), tag)
        return {"mode": "existing-tag", "collection": hit["title"],
                "collection_id": hit["id"], "product_tag": rule_tag,
                "note": f"existing collection \"{hit['title']}\" (rule TAG = {rule_tag})"}

    hit = by_title.get(brand.strip().lower())
    if hit:
        if hit["tags"]:
            return {"mode": "existing-title", "collection": hit["title"],
                    "collection_id": hit["id"], "product_tag": hit["tags"][0],
                    "note": f"existing collection \"{hit['title']}\" "
                            f"(rule TAG = {hit['tags'][0]}, differs from {tag})"}
        return {"mode": "skipped", "collection": hit["title"],
                "collection_id": hit["id"], "product_tag": None,
                "note": f"collection \"{hit['title']}\" exists but has no TAG rule "
                        f"(manual collection) - products would not join it"}

    return {"mode": "new", "collection": brand, "collection_id": None,
            "product_tag": tag, "note": f"new smart collection \"{brand}\" (TAG = {tag})"}


# ────────────────────────────────────────────────────────────────
# Run state (persisted to disk)
# ────────────────────────────────────────────────────────────────
class BulkRun:
    def __init__(self, data: dict) -> None:
        self.__dict__.update(data)

    # ── construction ──
    @classmethod
    def new(cls, input_name: str, keepa_name: str,
            seo_mode: bool, variant_mode: bool) -> "BulkRun":
        rid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        return cls({
            "id": rid,
            "created_at": time.time(),
            "updated_at": time.time(),
            "status": "resolving",
            "stop_requested": False,
            "input_name": input_name,
            "keepa_name": keepa_name,
            "seo_mode": seo_mode,
            "variant_mode": variant_mode,
            "chunk_size": max(1, int(settings.max_asins)),
            "brands": [],
            "chunks": [],
            "skipped": [],
            "results": {},          # asin -> per-ASIN outcome
            "logs": [],
            "current_job_id": None,
            "error": None,
        })

    @property
    def folder(self) -> Path:
        return BULK_DIR / self.id

    # ── persistence ──
    def save(self) -> None:
        self.updated_at = time.time()
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / "run.json.tmp"
        data = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.folder / "run.json")

    @classmethod
    def load(cls, run_id: str) -> Optional["BulkRun"]:
        if run_id in _RUNS:
            return _RUNS[run_id]
        p = BULK_DIR / run_id / "run.json"
        if not p.is_file():
            return None
        run = cls(json.loads(p.read_text(encoding="utf-8")))
        _RUNS[run_id] = run
        return run

    # ── logging ──
    def log(self, message: str, level: str = "info") -> None:
        self.logs.append({"time": datetime.now().strftime("%H:%M:%S"),
                          "level": level, "message": message})
        if len(self.logs) > 600:
            self.logs = self.logs[-600:]

    # ── views ──
    def totals(self) -> dict:
        ch = self.chunks
        done_asins = sum(len(c["asins"]) for c in ch if c["status"] == "done")
        total_asins = sum(len(c["asins"]) for c in ch)
        res = self.results.values()
        return {
            "brands": len(self.brands),
            "brands_skipped": sum(1 for b in self.brands if b["mode"] == "skipped"),
            "chunks": len(ch),
            "chunks_done": sum(1 for c in ch if c["status"] == "done"),
            "chunks_failed": sum(1 for c in ch if c["status"] == "failed"),
            "asins_total": total_asins,
            "asins_done": done_asins,
            "asins_skipped": len(self.skipped),
            "created": sum(1 for r in res if r.get("action") == "created"),
            "updated": sum(1 for r in res if r.get("action") == "updated"),
            "dead": sum(1 for r in res if r.get("dead")),
            "failed": sum(1 for r in res if r.get("ok") is False),
            "seo_ok": sum(1 for r in res if r.get("seo") is True),
            "seo_skipped": sum(1 for r in res if r.get("seo") == "skipped"),
        }

    def effective_status(self) -> str:
        if self.status in ACTIVE_STATES:
            t = _TASKS.get(self.id)
            if t is None or t.done():
                return "interrupted"
        return self.status

    def public(self, with_logs: bool = True) -> dict:
        out = {
            "id": self.id, "status": self.effective_status(), "created_at": self.created_at,
            "updated_at": self.updated_at, "input_name": self.input_name,
            "keepa_name": self.keepa_name, "seo_mode": self.seo_mode,
            "variant_mode": self.variant_mode, "chunk_size": self.chunk_size,
            "stop_requested": self.stop_requested, "error": self.error,
            "current_job_id": self.current_job_id, "totals": self.totals(),
        }
        if with_logs:
            out["brands"] = self.brands
            out["chunks"] = [{k: c[k] for k in ("n", "brand", "collection", "tag",
                                                  "status", "error")} | {"count": len(c["asins"])}
                             for c in self.chunks]
            out["logs"] = self.logs[-200:]
        return out


def list_runs() -> List[dict]:
    out = []
    for p in sorted(BULK_DIR.glob("*/run.json"), reverse=True)[:30]:
        run = BulkRun.load(p.parent.name)
        if run:
            out.append(run.public(with_logs=False))
    return out


def any_active() -> Optional[str]:
    for rid, t in _TASKS.items():
        if not t.done():
            return rid
    return None


# ────────────────────────────────────────────────────────────────
# Phases of a bulk run
# ────────────────────────────────────────────────────────────────
async def _prepare(run: BulkRun, csv_bytes: bytes, keepa_bytes: bytes) -> None:
    """Parse file, check Keepa, resolve collections, build chunks."""
    pairs, rejected = parse_asin_brand_csv(csv_bytes)
    run.log(f"Upload file: {len(pairs)} unique ASIN(s)")
    for r in rejected:
        run.skipped.append(r)
        run.log(f"  ⚠ {r['asin']} skipped - {r['reason']}", "warn")

    keepa_data = keepa_mod.load_keepa_csv(keepa_bytes)
    run.log(f"Keepa CSV: {len(keepa_data)} row(s)")
    in_keepa = []
    for asin, brand in pairs:
        if asin in keepa_data:
            in_keepa.append((asin, brand))
        else:
            run.skipped.append({"asin": asin, "brand": brand, "reason": "not in Keepa CSV"})
    miss = len(pairs) - len(in_keepa)
    if miss:
        run.log(f"  ⚠ {miss} ASIN(s) not in Keepa CSV - skipped", "warn")
    if not in_keepa:
        raise RuntimeError("No ASINs left - none of the uploaded ASINs are in the Keepa CSV")

    groups, empty_tag = group_by_brand(in_keepa)
    for r in empty_tag:
        run.skipped.append(r)
        run.log(f"  ⚠ {r['asin']} ({r['brand']}) skipped - {r['reason']}", "warn")
    run.log(f"Brands: {len(groups)}")

    run.log("Reading Shopify collections + their TAG rules ...")
    by_tag, by_title, total = await load_collection_index()
    run.log(f"  {total} collection(s) indexed")

    n = 0
    for g in groups:
        res = resolve_brand(g, by_tag, by_title)

        # Guard: an existing collection must be findable by Phase 2's title search,
        # otherwise Phase 2 would create a DUPLICATE collection with the same title.
        if res["mode"] in ("existing-tag", "existing-title"):
            found = await shopify.find_collection_by_title(res["collection"])
            if not found or found.get("id") != res["collection_id"]:
                res = {**res, "mode": "skipped",
                       "note": f"collection \"{res['collection']}\" not findable by title "
                               f"search - skipped to avoid creating a duplicate"}

        brand_row = {"brand": g["brand"], "tag": g["tag"], "count": len(g["asins"]), **res}
        run.brands.append(brand_row)

        if res["mode"] == "skipped":
            run.log(f"  ✖ {g['brand']} ({len(g['asins'])}) - {res['note']}", "warn")
            for a in g["asins"]:
                run.skipped.append({"asin": a, "brand": g["brand"], "reason": res["note"]})
            continue

        run.log(f"  ✔ {g['brand']} ({len(g['asins'])}) -> {res['note']}")
        size = run.chunk_size
        for i in range(0, len(g["asins"]), size):
            n += 1
            run.chunks.append({
                "n": n, "brand": g["brand"], "collection": res["collection"],
                "tag": res["product_tag"], "asins": g["asins"][i:i + size],
                "status": "pending", "job_id": None, "error": None,
                "started_at": None, "finished_at": None,
            })

    run.log(f"Plan: {len(run.chunks)} chunk(s) of up to {run.chunk_size}, "
            f"{sum(len(c['asins']) for c in run.chunks)} ASIN(s)")


def _record_chunk(run: BulkRun, chunk: dict, job) -> None:
    result = job.result or {}
    seo = {r.get("asin"): ("skipped" if r.get("skipped") else bool(r.get("ok")))
           for r in (result.get("seo_results") or [])
           if isinstance(r, dict) and r.get("asin")}
    # Why SEO failed / was skipped, per ASIN (report only - no logic change)
    seo_note = {}
    for r in (result.get("seo_results") or []):
        if isinstance(r, dict) and r.get("asin") and not r.get("ok"):
            seo_note[r["asin"]] = str(r.get("error") or (r.get("errors") or [""])[0] or "")[:200]
    # SEO phase crashed as a whole -> every ASIN in the chunk gets that error
    phase_err = ""
    for entry in getattr(job, "logs", []) or []:
        m = str(entry.get("message", ""))
        if "SEO error (products already saved)" in m or "SEO engine unavailable" in m:
            phase_err = m.strip()[:200]
    by_asin = {p["asin"]: p for p in (result.get("products") or [])}
    ran = set(job.asins)

    for a in chunk["asins"]:
        if a not in ran:
            run.results[a] = {"asin": a, "brand": chunk["brand"], "collection": chunk["collection"],
                              "tag": chunk["tag"], "ok": None, "note": "skipped by Phase 1 (Keepa)"}
            continue
        p = by_asin.get(a)
        if job.status != "done" or not p:
            run.results[a] = {"asin": a, "brand": chunk["brand"], "collection": chunk["collection"],
                              "tag": chunk["tag"], "ok": False,
                              "note": (p or {}).get("error") or job.error or "chunk failed"}
            continue
        run.results[a] = {
            "asin": a, "brand": chunk["brand"], "collection": chunk["collection"],
            "tag": chunk["tag"], "ok": p.get("ok"), "action": p.get("action"),
            "dead": p.get("dead"), "price": p.get("price"), "mrp": p.get("mrp"),
            "handle": p.get("handle"), "admin_url": p.get("admin_url"),
            "seo": (seo.get(a, False if phase_err else None)) if run.seo_mode else None,
            "seo_note": (seo_note.get(a) or (phase_err if a not in seo else "")) if run.seo_mode else "",
            "note": p.get("error") or p.get("reason") or "",
        }


async def _execute(run: BulkRun, csv_bytes: bytes, keepa_bytes: bytes) -> None:
    async with _RUN_LOCK:
        try:
            if not run.chunks and run.status == "resolving":
                await _prepare(run, csv_bytes, keepa_bytes)
            run.status = "running"
            run.save()

            total = len(run.chunks)
            for chunk in run.chunks:
                if chunk["status"] in ("done", "skipped"):
                    continue
                if run.stop_requested:
                    run.status = "stopped"
                    run.log("Stopped by user (between chunks). Resume any time.", "warn")
                    run.save()
                    return

                chunk["status"] = "running"
                chunk["started_at"] = time.time()
                chunk["error"] = None
                run.log(f"Chunk {chunk['n']}/{total} - {chunk['brand']} - "
                        f"{len(chunk['asins'])} ASIN(s) -> \"{chunk['collection']}\" "
                        f"(tag {chunk['tag']})")

                job = store.create(list(chunk["asins"]), chunk["collection"], chunk["tag"],
                                   variant_mode=run.variant_mode, seo_mode=run.seo_mode,
                                   price_overrides={})
                chunk["job_id"] = job.id
                run.current_job_id = job.id
                run.save()

                await run_job(job, keepa_bytes)          # ← the unchanged ten-asin pipeline

                chunk["finished_at"] = time.time()
                if job.status == "done":
                    chunk["status"] = "done"
                elif "No ASINs left" in (job.error or ""):
                    chunk["status"] = "skipped"
                    chunk["error"] = job.error
                else:
                    chunk["status"] = "failed"
                    chunk["error"] = job.error
                    run.log(f"  ✖ chunk {chunk['n']} failed: {job.error}", "error")
                _record_chunk(run, chunk, job)
                t = run.totals()
                run.log(f"  chunk {chunk['n']} {chunk['status']} - progress "
                        f"{t['asins_done']}/{t['asins_total']} ASIN(s)")
                run.current_job_id = None
                run.save()

            failed = sum(1 for c in run.chunks if c["status"] == "failed")
            run.status = "done"
            t = run.totals()
            run.log(f"BIG-B finished - {t['created']} created, {t['updated']} updated, "
                    f"{t['dead']} dead, {t['failed']} failed, {t['asins_skipped']} skipped"
                    + (f", {t['seo_ok']} SEO, {t['seo_skipped']} SEO skipped (no price)" if run.seo_mode else "")
                    + (f" | {failed} chunk(s) failed - press Resume to retry them" if failed else ""))
            run.save()

        except asyncio.CancelledError:
            run.status = "stopped"
            run.log("Run cancelled (server stopping). Resume any time.", "warn")
            run.save()
            raise
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.error = str(exc)
            run.log(f"BIG-B error: {exc}", "error")
            run.save()


# ────────────────────────────────────────────────────────────────
# Public entry points (used by bulk_routes)
# ────────────────────────────────────────────────────────────────
def start(csv_bytes: bytes, keepa_bytes: bytes, input_name: str, keepa_name: str,
          seo_mode: bool, variant_mode: bool) -> BulkRun:
    run = BulkRun.new(input_name, keepa_name, seo_mode, variant_mode)
    run.folder.mkdir(parents=True, exist_ok=True)
    (run.folder / "input.csv").write_bytes(csv_bytes)
    (run.folder / "keepa.csv").write_bytes(keepa_bytes)
    run.log(f"BIG-B run created - SEO {'ON' if seo_mode else 'OFF'}, "
            f"Variants {'ON' if variant_mode else 'OFF'}")
    run.save()
    _RUNS[run.id] = run
    _TASKS[run.id] = asyncio.create_task(_execute(run, csv_bytes, keepa_bytes))
    return run


def resume(run: BulkRun) -> BulkRun:
    csv_bytes = (run.folder / "input.csv").read_bytes()
    keepa_bytes = (run.folder / "keepa.csv").read_bytes()
    for c in run.chunks:
        if c["status"] in ("running", "failed"):
            c["status"] = "pending"
    run.stop_requested = False
    run.error = None
    if run.chunks:
        run.status = "running"
    else:
        run.status = "resolving"
        run.brands, run.skipped = [], []
    run.log("Resumed - done chunks are kept, pending/failed chunks run again")
    run.save()
    _TASKS[run.id] = asyncio.create_task(_execute(run, csv_bytes, keepa_bytes))
    return run


def request_stop(run: BulkRun) -> None:
    run.stop_requested = True
    if run.status in ("running", "resolving"):
        run.status = "stopping"
    run.log("Stop requested - the current chunk will finish first", "warn")
    run.save()


async def preview(csv_bytes: bytes, keepa_bytes: Optional[bytes]) -> dict:
    """Dry run: parse + group + resolve collections. No Shopify writes."""
    pairs, rejected = parse_asin_brand_csv(csv_bytes)
    keepa_missing = []
    if keepa_bytes:
        kd = keepa_mod.load_keepa_csv(keepa_bytes)
        keepa_missing = [a for a, _ in pairs if a not in kd]
        pairs = [(a, b) for a, b in pairs if a in kd]
    groups, empty_tag = group_by_brand(pairs)
    by_tag, by_title, total = await load_collection_index()
    brands = []
    for g in groups:
        res = resolve_brand(g, by_tag, by_title)
        brands.append({"brand": g["brand"], "tag": g["tag"], "count": len(g["asins"]), **res})
    return {
        "asins": len(pairs), "brands": brands, "collections_indexed": total,
        "rejected": rejected + empty_tag, "keepa_missing": keepa_missing,
        "chunk_size": max(1, int(settings.max_asins)),
    }


def report_csv(run: BulkRun) -> str:
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["ASIN", "Brand", "Collection", "Tag", "Status", "Action",
                "Dead", "Price", "MRP", "SEO", "Handle", "Admin URL", "Note", "SEO Note"])
    for r in run.results.values():
        status = "ok" if r.get("ok") else ("skipped" if r.get("ok") is None else "failed")
        w.writerow([r.get("asin"), r.get("brand"), r.get("collection"), r.get("tag"), status,
                    r.get("action") or "", r.get("dead") if r.get("dead") is not None else "",
                    r.get("price") or "", r.get("mrp") or "",
                    "" if r.get("seo") is None else ("skipped" if r["seo"] == "skipped" else ("yes" if r["seo"] else "no")),
                    r.get("handle") or "", r.get("admin_url") or "", r.get("note") or "",
                    r.get("seo_note") or ""])
    for s in run.skipped:
        w.writerow([s.get("asin"), s.get("brand"), "", "", "skipped",
                    "", "", "", "", "", "", "", s.get("reason"), ""])
    return buf.getvalue()

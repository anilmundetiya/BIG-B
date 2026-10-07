"""
In-memory job store + live event queues for SSE.

One job at a time is the expected usage, but multiple are supported.
Jobs are NOT persisted — restarting the server loses them. That's fine:
every phase writes through to Shopify, which is the durable store.
"""
import asyncio
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

PHASES = [
    (1, "Validate"),
    (2, "Collection"),
    (3, "Scrape"),
    (4, "Upload"),
    (5, "Pricing"),
    (6, "Done"),
]


class Job:
    def __init__(self, asins: List[str], collection: str, tag: str,
                 price_overrides: Optional[Dict[str, float]] = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.asins = asins
        self.collection = collection
        self.tag = tag
        self.price_overrides = price_overrides or {}

        self.status = "queued"          # queued | running | done | failed
        self.phase = 0
        self.phase_name = "Queued"
        self.progress = 0               # 0-100
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None

        self.logs: List[dict] = []
        self.result: Optional[dict] = None

        # Per-ASIN working state
        self.scraped: Dict[str, dict] = {}
        self.uploaded: Dict[str, dict] = {}
        self.priced: Dict[str, dict] = {}
        self.failures: Dict[str, str] = {}

        self.collection_info: Optional[dict] = None
        self.variant_mode: bool = False
        self.seo_mode: bool = False
        self.sibling_groups: List[dict] = []
        self.seo_results: List[dict] = []
        self._queues: List[asyncio.Queue] = []

    # ── Event plumbing ──────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues.append(q)
        # Replay existing logs so a late subscriber sees the whole run
        for entry in self.logs:
            q.put_nowait({"type": "log", **entry})
        q.put_nowait({"type": "state", **self.state()})
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def _emit(self, event: dict) -> None:
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    # ── Logging ─────────────────────────────────────────────────
    def log(self, message: str, level: str = "info") -> None:
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
        self.logs.append(entry)
        if len(self.logs) > 800:
            self.logs = self.logs[-800:]
        self._emit({"type": "log", **entry})

    def set_phase(self, number: int, name: str, progress: Optional[int] = None) -> None:
        self.phase = number
        self.phase_name = name
        if progress is not None:
            self.progress = max(0, min(100, int(progress)))
        self._emit({"type": "state", **self.state()})

    def set_progress(self, progress: int) -> None:
        self.progress = max(0, min(100, int(progress)))
        self._emit({"type": "state", **self.state()})

    def finish(self, result: dict) -> None:
        self.status = "done"
        self.result = result
        self.progress = 100
        self.phase = 6
        self.phase_name = "Done"
        self.finished_at = time.time()
        self._emit({"type": "done", "result": result})

    def fail(self, error: str) -> None:
        self.status = "failed"
        self.error = error
        self.finished_at = time.time()
        self.log(error, "error")
        self._emit({"type": "failed", "error": error})

    # ── Snapshot ────────────────────────────────────────────────
    def state(self) -> dict:
        return {
            "job_id": self.id,
            "status": self.status,
            "phase": self.phase,
            "phase_name": self.phase_name,
            "phase_total": len(PHASES),
            "progress": self.progress,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            "asin_count": len(self.asins),
            "error": self.error,
        }

    def snapshot(self) -> dict:
        return {**self.state(), "logs": self.logs[-200:], "result": self.result}


class JobStore:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}

    def create(self, asins: List[str], collection: str, tag: str,
               variant_mode: bool = False, seo_mode: bool = False,
               price_overrides: Optional[Dict[str, float]] = None) -> Job:
        job = Job(asins, collection, tag, price_overrides=price_overrides)
        job.variant_mode = variant_mode
        job.seo_mode = seo_mode
        self._jobs[job.id] = job
        # Keep only the 20 most recent
        if len(self._jobs) > 20:
            oldest = sorted(self._jobs.values(), key=lambda j: j.started_at)[:-20]
            for j in oldest:
                self._jobs.pop(j.id, None)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def recent(self, n: int = 10) -> List[dict]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)
        return [j.state() for j in jobs[:n]]


store = JobStore()

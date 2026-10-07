"""FastAPI application — BIG-B (bulk ASIN listing, built on the ten-asin pipeline)."""
from fastapi import FastAPI

from . import __version__
from .bulk_routes import router as bulk_router
from .routes import router

app = FastAPI(
    title="BIG-B",
    version=__version__,
    docs_url="/docs",
    redoc_url=None,
)

# bulk_router first: its "/" (bulk page) wins over routes.py's "/" (now at /single)
app.include_router(bulk_router)
app.include_router(router)

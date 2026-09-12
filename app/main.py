"""FastAPI application factory and entry point.

Run with::

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from fastapi import FastAPI

from .routers import catalogs, correction, parts, plans, segments
from .runtime import get_store

app = FastAPI(
    title="Sheet-metal bend sequence planner",
    version="1.2.0",
    description="Progressive forming geometry, deterministic bend-order "
                "search, machine/collision/backgauge checks, segmented "
                "tool layouts with tool-change optimisation, sealed "
                "process cards and first-piece springback correction.")

app.include_router(parts.router)
app.include_router(plans.router)
app.include_router(catalogs.router)
app.include_router(segments.router)
app.include_router(correction.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "db": get_store().path}

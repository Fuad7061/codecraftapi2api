"""Codecraft2API — FastAPI app. DB auto-initialises on startup."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import api, dashboard, db, pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    tasks = [asyncio.create_task(f()) for f in (pool.keepalive_loop, pool.plan_loop, pool.cleanup_loop)]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Codecraft2API", version="1.0.0", lifespan=lifespan, docs_url="/docs", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(api.router)
app.include_router(dashboard.router)


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    detail = exc.detail
    body = detail if isinstance(detail, dict) and "error" in detail else {"error": {"message": str(detail)}}
    return JSONResponse(body, status_code=exc.status_code)


@app.get("/dashboard", include_in_schema=False)
@app.get("/dashboard/", include_in_schema=False)
async def legacy_dashboard():
    return RedirectResponse("/")


@app.get("/dashboard/login", include_in_schema=False)
async def legacy_login():
    return RedirectResponse("/login")

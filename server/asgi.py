#!/usr/bin/env python3
"""ASGI production entrypoint for self-hosted Second Hello deployments."""
from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

try:
    import main as core
    from production_server import configuration, workflow_events
except ImportError:  # package-style execution
    from . import main as core
    from .production_server import configuration, workflow_events


CONFIG = configuration()
WEB_ROOT: Path = CONFIG["webRoot"]
app = FastAPI(
    title="Second Hello Agent",
    version="1.0.0",
    docs_url="/docs" if CONFIG["environment"] != "production" else None,
    redoc_url=None,
)

if CONFIG["origins"]:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CONFIG["origins"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-SecondHello-Confirm"],
    )

if (WEB_ROOT / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_ROOT / "assets"), name="assets")


@app.middleware("http")
async def request_context(request: Request, call_next: Any) -> Any:
    rid = request.headers.get("X-Request-ID") or str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def require_auth(request: Request) -> None:
    token = CONFIG["token"]
    if not token:
        if CONFIG["environment"] == "production":
            raise HTTPException(status_code=503, detail="authentication_not_configured")
        return
    supplied = request.headers.get("Authorization", "")
    if not hmac.compare_digest(supplied, f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="authentication_required")


@app.get("/health")
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {**core.health(), "server": "asgi", "ok": True}


@app.get("/readyz")
@app.get("/api/readyz")
async def readyz() -> JSONResponse:
    checks = {
        "workflow": core.GRAPH is not None or hasattr(core, "run_local"),
        "storage": bool(core.BACKEND),
        "auth": bool(CONFIG["token"]) or CONFIG["environment"] != "production",
    }
    return JSONResponse({"ok": all(checks.values()), "checks": checks}, status_code=200 if all(checks.values()) else 503)


@app.get("/api/memory")
@app.get("/api/memory/export")
async def memory(request: Request) -> dict[str, Any]:
    require_auth(request)
    return await run_in_threadpool(core.BACKEND.load)


@app.delete("/api/memory")
async def delete_memory(request: Request, x_secondhello_confirm: str | None = Header(default=None)) -> dict[str, Any]:
    require_auth(request)
    if x_secondhello_confirm != "DELETE_ALL":
        raise HTTPException(status_code=428, detail="explicit_delete_confirmation_required")
    await run_in_threadpool(core.BACKEND.erase_all)
    return {"ok": True, "deleted": True}


@app.get("/api/voice/signed-url")
async def signed_url(request: Request) -> JSONResponse:
    require_auth(request)
    status, payload = await run_in_threadpool(core.elevenlabs_signed_url)
    return JSONResponse(payload, status_code=status)


@app.post("/api/voice/scribe-token")
async def scribe_token(request: Request) -> JSONResponse:
    require_auth(request)
    status, payload = await run_in_threadpool(core.elevenlabs_scribe_token)
    return JSONResponse(payload, status_code=status)


@app.post("/api/workflow")
async def workflow(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    require_auth(request)
    return await run_in_threadpool(core.run_workflow, payload)


@app.post("/api/workflow/events")
async def workflow_events_endpoint(request: Request, payload: dict[str, Any]) -> StreamingResponse:
    require_auth(request)

    def stream():
        for event in workflow_events(payload):
            yield f"event: {event.get('type', 'message')}\ndata: {__import__('json').dumps(event, default=str, separators=(',', ':'))}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-store",
        "Connection": "close",
        "X-Accel-Buffering": "no",
    })


@app.get("/{path:path}")
async def web(path: str = "") -> FileResponse:
    candidate = (WEB_ROOT / (path or "index.html")).resolve()
    if WEB_ROOT not in candidate.parents and candidate != WEB_ROOT:
        raise HTTPException(status_code=404, detail="not_found")
    if not candidate.is_file():
        candidate = WEB_ROOT / "index.html"
    if not candidate.is_file():
        raise HTTPException(status_code=503, detail="web_build_not_found")
    return FileResponse(candidate)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("SECONDHELLO_HOST", "127.0.0.1"), port=int(os.getenv("SECONDHELLO_PORT", "8765")))

#!/usr/bin/env python3
"""Production HTTP boundary for the self-hosted Second Hello agent.

The workflow engine remains in ``main.py``. This module adds the boundary that
the native client and web client need in a real deployment: bearer auth,
bounded request bodies, CORS policy, structured errors, SSE workflow events,
readiness checks, and safe static-file serving.

It intentionally uses the Python standard library so the self-hosted runtime
does not acquire a second web framework dependency. Put it behind a TLS
reverse proxy for internet-facing deployments.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit
from uuid import uuid4


def _core_module() -> Any:
    # ``python server/main.py`` has already loaded the workflow as __main__.
    # Reusing that module avoids creating a second provider/backend instance.
    running_main = sys.modules.get("__main__")
    if running_main is not None and hasattr(running_main, "run_workflow"):
        return running_main
    import main

    return main


CORE = _core_module()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = PROJECT_ROOT / "web" / "dist"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def configuration() -> dict[str, Any]:
    environment = env("SECONDHELLO_ENV", "development").lower()
    token = env("SECONDHELLO_AUTH_TOKEN")
    if environment == "production" and not token:
        raise RuntimeError("SECONDHELLO_AUTH_TOKEN is required when SECONDHELLO_ENV=production")
    origins = [item.strip() for item in env("SECONDHELLO_CORS_ORIGINS").split(",") if item.strip()]
    return {
        "environment": environment,
        "token": token,
        "origins": origins,
        "maxBodyBytes": max(16_384, int(env("SECONDHELLO_MAX_BODY_BYTES", "1048576"))),
        "webRoot": Path(env("SECONDHELLO_WEB_ROOT") or str(WEB_ROOT)).resolve(),
    }


def request_id() -> str:
    return str(uuid4())


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")


def workflow_events(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Stream durable workflow milestones without exposing provider secrets."""
    yield {"type": "workflow.started", "workflow": "consent-first-networking", "action": payload.get("action")}
    initial = {**payload, "trace": [], "ok": True}
    try:
        if CORE.GRAPH is None:
            final = CORE.run_workflow(payload)
            for item in final.get("trace", []):
                yield {"type": "node.completed", "node": item.get("tool"), "trace": item}
            yield {"type": "workflow.completed", "result": final}
            return

        merged: dict[str, Any] = dict(initial)
        for update in CORE.GRAPH.stream(initial, stream_mode="updates"):
            if not isinstance(update, dict):
                continue
            for node, patch in update.items():
                if isinstance(patch, dict):
                    merged.update(patch)
                    traces = patch.get("trace") or []
                    event: dict[str, Any] = {"type": "node.completed", "node": node}
                    if traces:
                        event["trace"] = traces[-1]
                    if "profile" in patch:
                        event["profile"] = patch["profile"]
                    if "research" in patch:
                        event["research"] = patch["research"]
                    if "opportunities" in patch:
                        event["opportunities"] = patch["opportunities"]
                    yield event
        yield {"type": "workflow.completed", "result": CORE.response(merged)}
    except Exception as error:  # the client gets a terminal event, never a hanging spinner
        yield {
            "type": "workflow.failed",
            "error": "workflow_failed",
            "message": str(error),
        }


class SecondHelloHandler(BaseHTTPRequestHandler):
    server_version = "SecondHello/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def config(self) -> dict[str, Any]:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        payload = {"event": "http.request", "message": format % args, "requestId": getattr(self, "rid", None)}
        print(json.dumps(payload, separators=(",", ":")), flush=True)

    def origin_allowed(self) -> bool:
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        origins = self.config["origins"]
        if self.config["environment"] != "production" and not origins:
            return True
        host = self.headers.get("Host", "")
        same_origin = {f"http://{host}", f"https://{host}"}
        return origin in origins or origin in same_origin

    def add_common_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        same_origin = {f"http://{host}", f"https://{host}"}
        if origin and (origin in self.config["origins"] or origin in same_origin or (self.config["environment"] != "production" and not self.config["origins"])):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Request-ID", self.rid)

    def fail(self, status: int, reason: str, message: str | None = None) -> None:
        payload = {"ok": False, "error": reason, "requestId": self.rid}
        if message and self.config["environment"] != "production":
            payload["message"] = message
        body = json_bytes(payload)
        self.send_response(status)
        self.add_common_headers()
        self.send_header("Content-Type", "application/problem+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorize(self) -> bool:
        token = self.config["token"]
        if not token:
            return self.config["environment"] != "production"
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if not hmac.compare_digest(supplied, expected):
            self.fail(HTTPStatus.UNAUTHORIZED, "authentication_required")
            return False
        return True

    def read_payload(self) -> dict[str, Any] | None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.fail(HTTPStatus.BAD_REQUEST, "invalid_content_length")
            return None
        if size <= 0 or size > self.config["maxBodyBytes"]:
            self.fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_body_too_large")
            return None
        try:
            value = json.loads(self.rfile.read(size))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.fail(HTTPStatus.BAD_REQUEST, "invalid_json")
            return None
        if not isinstance(value, dict):
            self.fail(HTTPStatus.BAD_REQUEST, "json_object_required")
            return None
        return value

    def send_json(self, status: int, payload: Any) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.add_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.rid = request_id()
        if not self.origin_allowed():
            self.fail(HTTPStatus.FORBIDDEN, "origin_not_allowed")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.add_common_headers()
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type,X-SecondHello-Confirm")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        self.rid = request_id()
        if not self.origin_allowed():
            self.fail(HTTPStatus.FORBIDDEN, "origin_not_allowed")
            return
        path = urlsplit(self.path).path
        if path in {"/health", "/api/health"}:
            self.send_json(HTTPStatus.OK, {**CORE.health(), "requestId": self.rid})
            return
        if path in {"/readyz", "/api/readyz"}:
            checks = {
                "workflow": CORE.GRAPH is not None or hasattr(CORE, "run_local"),
                "storage": bool(CORE.BACKEND),
                "auth": bool(self.config["token"]) or self.config["environment"] != "production",
            }
            status = HTTPStatus.OK if all(checks.values()) else HTTPStatus.SERVICE_UNAVAILABLE
            self.send_json(status, {"ok": status == HTTPStatus.OK, "checks": checks, "requestId": self.rid})
            return
        if path.startswith("/api/") or path in {"/memory", "/elevenlabs/signed-url"}:
            if not self.authorize():
                return
            if path in {"/api/memory", "/memory", "/api/memory/export"}:
                self.send_json(HTTPStatus.OK, CORE.BACKEND.load())
                return
            if path in {"/api/voice/signed-url", "/elevenlabs/signed-url"}:
                status, payload = CORE.elevenlabs_signed_url()
                self.send_json(status, payload)
                return
            if path == "/api/voice/scribe-token":
                status, payload = CORE.elevenlabs_scribe_token()
                self.send_json(status, payload)
                return
            self.fail(HTTPStatus.NOT_FOUND, "not_found")
            return
        self.serve_static(path)

    def do_POST(self) -> None:
        self.rid = request_id()
        if not self.origin_allowed():
            self.fail(HTTPStatus.FORBIDDEN, "origin_not_allowed")
            return
        if not self.authorize():
            return
        path = urlsplit(self.path).path
        if path == "/api/voice/scribe-token":
            status, response = CORE.elevenlabs_scribe_token()
            self.send_json(status, response)
            return
        if path not in {"/api/workflow", "/workflow", "/api/workflow/events"}:
            self.fail(HTTPStatus.NOT_FOUND, "not_found")
            return
        payload = self.read_payload()
        if payload is None:
            return
        try:
            if path == "/api/workflow/events":
                self.send_events(payload)
            else:
                self.send_json(HTTPStatus.OK, {**CORE.run_workflow(payload), "requestId": self.rid})
        except Exception as error:
            self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, "workflow_failed", str(error))

    def do_DELETE(self) -> None:
        self.rid = request_id()
        if not self.origin_allowed():
            self.fail(HTTPStatus.FORBIDDEN, "origin_not_allowed")
            return
        if not self.authorize():
            return
        if urlsplit(self.path).path != "/api/memory":
            self.fail(HTTPStatus.NOT_FOUND, "not_found")
            return
        if self.headers.get("X-SecondHello-Confirm") != "DELETE_ALL":
            self.fail(HTTPStatus.PRECONDITION_REQUIRED, "explicit_delete_confirmation_required")
            return
        try:
            CORE.BACKEND.erase_all()
            self.send_json(HTTPStatus.OK, {"ok": True, "deleted": True, "requestId": self.rid})
        except Exception as error:
            self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, "delete_failed", str(error))

    def send_events(self, payload: dict[str, Any]) -> None:
        self.send_response(HTTPStatus.OK)
        self.add_common_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        for event in workflow_events(payload):
            event["requestId"] = self.rid
            body = f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, default=str, separators=(',', ':'))}\n\n".encode()
            self.wfile.write(body)
            self.wfile.flush()
        self.close_connection = True

    def serve_static(self, path: str) -> None:
        root = self.config["webRoot"]
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (root / relative).resolve()
        if root not in candidate.parents and candidate != root:
            self.fail(HTTPStatus.NOT_FOUND, "not_found")
            return
        if not candidate.is_file():
            candidate = root / "index.html"
        if not candidate.is_file():
            self.fail(HTTPStatus.NOT_FOUND, "web_build_not_found")
            return
        content_type = "text/html; charset=utf-8" if candidate.suffix == ".html" else "application/octet-stream"
        if candidate.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif candidate.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif candidate.suffix == ".svg":
            content_type = "image/svg+xml"
        body = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.add_common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self' https: wss:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data: https:")
        self.end_headers()
        self.wfile.write(body)


def serve(host: str | None = None, port: int | None = None) -> None:
    config = configuration()
    bind_host = host or env("SECONDHELLO_HOST", "127.0.0.1")
    bind_port = port or int(env("SECONDHELLO_PORT", "8765"))
    server = ThreadingHTTPServer((bind_host, bind_port), SecondHelloHandler)
    server.config = config  # type: ignore[attr-defined]
    server.daemon_threads = True
    server.request_queue_size = 128
    print(json.dumps({
        "service": "secondhello",
        "listen": f"{bind_host}:{bind_port}",
        "environment": config["environment"],
        "storage": CORE.BACKEND.mode,
        "provider": CORE.PROVIDER.name,
        "auth": bool(config["token"]),
        "webRoot": str(config["webRoot"]),
    }, separators=(",", ":")), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()

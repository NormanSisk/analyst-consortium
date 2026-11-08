"""The setup screen's local web server (a local addition).

Safe by design, because it can write keys:

* it listens on 127.0.0.1 only, so nothing off this computer can reach it;
* every request must carry the right ``Host`` (blocks DNS-rebinding tricks) and, for changes, a matching ``Origin``;
* every ``/api`` call needs a random token that is handed only to the page this server serves (blocks other websites
  open in your browser from calling it);
* a strict content-security policy, no cache, and no secret is ever sent to the page: it learns only whether one is set.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import socket
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import tradingagents
from tradingagents.ui import settings_store as store

STATIC = Path(__file__).with_name("static")
TOKEN_HEADER = "x-council-token"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PASSES_OPTIONS = [1, 3, 5, 7, 9]
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
_LOCAL = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(?::(\d+))?$")


class SettingsUpdate(BaseModel):
    updates: dict[str, str | None] = Field(default_factory=dict, max_length=40)


class DoctorRequest(BaseModel):
    live_ai: bool = True
    offline: bool = False
    ids: list[str] | None = Field(default=None, max_length=80)


def _host_ok(host: str, port: int | None) -> bool:
    m = _LOCAL.match(host or "")
    return bool(m) and (port is None or m.group(2) is None or int(m.group(2)) == port)


def build_state(dotenv_path: Path | None = None) -> dict:
    """Everything the page needs to draw the form. Secrets appear only as set / not set."""
    from tradingagents.default_config import build_default_config
    from tradingagents.llm_clients.api_key_env import get_api_key_env
    from tradingagents.llm_clients.model_catalog import get_known_models
    from tradingagents.llm_clients.openai_client import SIGNED_IN_PROVIDERS

    cfg = build_default_config()
    known = get_known_models()
    path = dotenv_path or store.locate_dotenv()
    return {
        "version": tradingagents.__version__,
        "dotenv": {"exists": path.exists(), "path": str(path)},
        "settings": store.current(),
        "effective": {
            "provider": str(cfg.get("llm_provider") or ""),
            "quick": str(cfg.get("quick_think_llm") or ""),
            "deep": str(cfg.get("deep_think_llm") or ""),
            "passes": int(cfg.get("research_passes") or 1),
        },
        "choices": {
            "providers": [{"id": p, "key_env": get_api_key_env(p), "sign_in": p in SIGNED_IN_PROVIDERS}
                          for p in store.providers()],
            "models": {p: list(known.get(p, [])) for p in store.providers()},
            "passes": PASSES_OPTIONS,
        },
    }


def create_app(token: str, dotenv_path: Path | None = None, port: int | None = None) -> FastAPI:
    app = FastAPI(title="Analyst Council setup", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.doctor_lock = asyncio.Lock()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not _host_ok(request.headers.get("host", ""), port):
            return JSONResponse({"detail": "This server answers only on 127.0.0.1 or localhost."}, status_code=400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and not _host_ok(urlparse(origin).netloc, port):
                return JSONResponse({"detail": "Requests from other sites are refused."}, status_code=403)
        if request.url.path.startswith("/api/"):
            given = request.headers.get(TOKEN_HEADER, "")
            if not secrets.compare_digest(given.encode(), token.encode()):
                return JSONResponse({"detail": "Missing or wrong session token. Reload the page."}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def page() -> HTMLResponse:
        html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__SESSION_TOKEN__", token)
        return HTMLResponse(html)

    @app.get("/api/state")
    async def state() -> dict:
        return build_state(dotenv_path)

    @app.post("/api/settings")
    async def save_settings(body: SettingsUpdate):
        try:
            result = store.save(body.updates, dotenv_path)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        return {"changed": result["changed"], "path": result["path"], "state": build_state(dotenv_path)}

    @app.post("/api/doctor")
    async def run_checks(body: DoctorRequest) -> dict:
        from tradingagents.doctor import run_doctor
        async with app.state.doctor_lock:        # one set of live tests at a time
            report = await run_in_threadpool(lambda: run_doctor(
                offline=body.offline, live_ai=body.live_ai, ids=body.ids))
        return report.to_dict()

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


def find_free_port(start: int = DEFAULT_PORT, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, port)) != 0:      # nothing is listening there
                return port
    raise OSError(f"No free port from {start} to {start + tries - 1}.")


def run_server(port: int | None = None, open_browser: bool = True, dotenv_path: Path | None = None) -> None:
    import uvicorn

    port = find_free_port(port or DEFAULT_PORT)
    app = create_app(secrets.token_urlsafe(24), dotenv_path, port)
    url = f"http://{HOST}:{port}/"
    print(f"Analyst Council setup screen: {url}\nPress Ctrl+C to stop. It answers only on this computer.")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=port, log_level="warning", access_log=False)

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
import os
import re
import secrets
import socket
import threading
import urllib.request
import webbrowser
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

import tradingagents
from tradingagents.agents.style import style_name
from tradingagents.ui import exports, picker_jobs
from tradingagents.ui import runs as run_tools
from tradingagents.ui import settings_store as store

STATIC = Path(__file__).with_name("static")
ASSETS = Path(__file__).resolve().parent.parent / "assets"             # the program's own pictures (the logo), shared with the reports
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


class RunRequest(BaseModel):
    """The run form, as the page sends it. ``runs.clean_request`` does the real checking."""

    tickers: list[str] = Field(max_length=run_tools.MAX_TICKERS * 4)
    date: str = Field(max_length=10)
    analysts: list[str] = Field(max_length=8)
    depth: int
    passes: int
    language: str = Field(default="English", max_length=60)
    style: str = Field(default="standard", max_length=20)
    holdings: str | None = Field(default=None, max_length=200)
    selloff: bool = True
    scorecard: bool = True
    open_report: bool = True


class ExportRequest(BaseModel):
    format: str = Field(max_length=8)
    ids: list[str] = Field(default_factory=list, max_length=run_tools.MAX_WAITING * 4)


class PickerSettingsIn(BaseModel):
    """The few picker settings the page may change, in the picker's own units (a fraction, dollars). Anything else is refused."""

    model_config = ConfigDict(extra="forbid")
    below_high: float | None = None
    min_market_cap: float | None = None
    min_avg_volume: float | None = None
    universe_limit: int | None = None
    top: int | None = None


class RangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: float | None = None
    max: float | None = None


class CustomIn(BaseModel):
    """The filters of a custom search, in the units the form shows. ``picker.custom.from_request`` does the real checking."""

    model_config = ConfigDict(extra="forbid")
    ranges: dict[str, RangeIn] = Field(default_factory=dict, max_length=60)
    flags: list[str] = Field(default_factory=list, max_length=10)
    sectors: list[str] = Field(default_factory=list, max_length=12)
    otc: bool = False
    exclude: str = Field(default="", max_length=5000)
    min_conditions: int | None = None
    sort: str = Field(default="conditions", max_length=20)
    examine: int | None = None
    check: int | None = None
    show: int | None = None


class PickerRequest(BaseModel):
    """A stock search, as the page sends it. ``picker_jobs.PickerManager.start`` does the real checking."""

    recipe: str = Field(max_length=20)
    text: str = Field(default="", max_length=picker_jobs.MAX_TEXT * 2)
    settings: PickerSettingsIn = Field(default_factory=PickerSettingsIn)
    record: bool = True
    custom: CustomIn | None = None


class PickerCountRequest(BaseModel):
    custom: CustomIn = Field(default_factory=CustomIn)


class PickerExportRequest(BaseModel):
    id: str = Field(max_length=40)


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
            "styles": [{"id": s, "label": run_tools.STYLE_LABELS[s], "help": run_tools.STYLE_HELP[s]} for s in run_tools.STYLES],
        },
    }


def holdings_folders(cfg: dict) -> list[Path]:
    """Where holdings files are looked for: next to the results folder (the data folder) and in the program's home."""
    from tradingagents.default_config import _TRADINGAGENTS_HOME

    folders = [Path(cfg["results_dir"]).parent, Path(_TRADINGAGENTS_HOME)]
    return [f for i, f in enumerate(folders) if f not in folders[:i]]


def build_run_options(manager: run_tools.RunManager | None = None) -> dict:
    """What the run form offers and starts with, from the settings in force right now."""
    from tradingagents.default_config import build_default_config
    from tradingagents.llm_clients.api_key_env import get_api_key_env
    from tradingagents.llm_clients.openai_client import SIGNED_IN_PROVIDERS

    cfg = build_default_config()
    provider, quick, deep = (str(cfg.get(k) or "") for k in ("llm_provider", "quick_think_llm", "deep_think_llm"))
    key_env = get_api_key_env(provider)
    passes = int(cfg.get("research_passes") or 1)
    depth = int(cfg.get("max_debate_rounds") or 1)
    options = sorted({*PASSES_OPTIONS, passes})
    books = run_tools.find_holdings(holdings_folders(cfg))
    language = str(cfg.get("output_language") or "English")
    env = os.environ
    return {
        "today": date.today().isoformat(),
        "analysts": [{"id": k, "label": label, "help": text} for k, label, text in run_tools.ANALYSTS],
        "depths": [{"value": v, "label": label, "help": text} for v, label, text in run_tools.DEPTHS],
        "passes": options,
        "costs": {str(n): run_tools.estimate(provider, quick, deep, n) for n in options},
        "languages": list(run_tools.LANGUAGES),
        "styles": [{"id": s, "label": run_tools.STYLE_LABELS[s], "help": run_tools.STYLE_HELP[s]} for s in run_tools.STYLES],
        "holdings": [{"id": name, "positions": b["positions"], "cash": b["cash"], "currency": b["currency"],
                      "profile": b["profile"], "folder": b["folder"]} for name, b in books.items()],
        "holdings_folders": [str(f) for f in holdings_folders(cfg)],
        "defaults": {"depth": depth if depth in run_tools.DEPTH_VALUES else 1, "passes": passes, "language": language,
                     "open_report": True, "style": style_name(),
                     "selloff": env.get("TRADINGAGENTS_NO_SELLOFF_RULES") != "1",
                     "scorecard": env.get("TRADINGAGENTS_NO_SCORECARD") != "1"},
        "provider": {"id": provider, "quick": quick, "deep": deep, "key_env": key_env,
                     "key_set": bool(key_env and env.get(key_env)) or (key_env is None),
                     "sign_in": provider in SIGNED_IN_PROVIDERS},
        "max_tickers": run_tools.MAX_TICKERS,
        "busy": bool(manager and manager.busy()),
    }


def create_app(token: str, dotenv_path: Path | None = None, port: int | None = None,
               runs: run_tools.RunManager | None = None, picker: picker_jobs.PickerManager | None = None) -> FastAPI:
    """The setup and run screen. ``runs`` is the queue of analyses and ``picker`` the stock search; by default each is made on first use."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if _app.state.runs is not None:
            await run_in_threadpool(_app.state.runs.shutdown)       # end a running analysis when the server closes

    app = FastAPI(title="Analyst Consortium", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.doctor_lock = asyncio.Lock()
    app.state.runs = runs
    app.state.runs_lock = threading.Lock()

    def manager() -> run_tools.RunManager:
        with app.state.runs_lock:
            if app.state.runs is None:
                from tradingagents.default_config import build_default_config

                cfg = build_default_config()
                folders = holdings_folders(cfg)
                app.state.runs = run_tools.RunManager(
                    cfg["results_dir"], holdings=lambda: {k: v["path"] for k, v in run_tools.find_holdings(folders).items()})
            return app.state.runs

    app.state.picker = picker

    def searcher() -> picker_jobs.PickerManager:
        with app.state.runs_lock:
            if app.state.picker is None:
                from tradingagents.picker.folders import picker_folder

                app.state.picker = picker_jobs.PickerManager(picker_folder())
            return app.state.picker

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

    @app.get("/logo-emblem.svg")
    async def logo_emblem() -> Response:
        """The symbol of the logo, shown to the left of the app's name (the lettering is left out: the name sits beside it)."""
        return Response((ASSETS / "analyst-consortium-emblem.svg").read_bytes(), media_type="image/svg+xml")

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

    @app.get("/api/run-options")
    async def run_options() -> dict:
        return await run_in_threadpool(lambda: build_run_options(app.state.runs))

    @app.get("/api/runs")
    async def list_runs() -> dict:
        return {"runs": await run_in_threadpool(manager().listing)}

    @app.post("/api/runs", status_code=202)
    async def start_runs(body: RunRequest):
        def start():
            options = build_run_options(app.state.runs)
            provider = options["provider"]
            if provider["key_env"] and not provider["key_set"]:
                raise run_tools.RequestError(
                    f"No key is saved for {provider['id']} yet. Open Setup and settings, paste the key and save.")
            if not (provider["quick"] and provider["deep"]):
                raise run_tools.RequestError("Choose a quick model and a deep model under Setup and settings first.")
            from tradingagents.default_config import build_default_config

            books = {k: v["path"] for k, v in run_tools.find_holdings(holdings_folders(build_default_config())).items()}
            request = run_tools.clean_request(body.model_dump(), today=date.today(), holdings=books)
            return manager().submit(request, provider["id"], provider["quick"], provider["deep"])

        try:
            made = await run_in_threadpool(start)
        except run_tools.RequestError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        return {"started": [job.id for job in made]}

    @app.post("/api/runs/{job_id}/cancel")
    async def cancel_run(job_id: str):
        if not run_tools.JOB_ID.fullmatch(job_id) or not await run_in_threadpool(manager().cancel, job_id):
            return JSONResponse({"detail": "That analysis is not waiting or running."}, status_code=404)
        return {"stopped": job_id}

    @app.post("/api/runs/{job_id}/remove")
    async def remove_run(job_id: str):
        """Take one finished, failed or stopped analysis off the list. Its saved report and run logs are left alone."""
        if not run_tools.JOB_ID.fullmatch(job_id) or not await run_in_threadpool(manager().forget, job_id):
            return JSONResponse({"detail": "That analysis is not on the list, or it is still waiting or running."},
                                status_code=404)
        return {"removed": job_id}

    @app.post("/api/runs/clear-finished")
    async def clear_finished_runs():
        """Clear every finished, failed and stopped analysis from the list; waiting and running ones stay."""
        return {"removed": await run_in_threadpool(manager().forget_finished)}

    @app.get("/api/runs/{job_id}/live")
    async def live_view(job_id: str, after: int = 0, have: str = ""):
        """New events since byte offset ``after``, each team member's state, and reports (text only when changed)."""
        held = {}
        for item in have.split(",")[:30]:
            key, _, rev = item.partition(":")
            if re.fullmatch(r"[a-z]{2,12}", key) and re.fullmatch(r"[0-9a-f]{1,8}-\d{1,9}", rev):
                held[key] = rev
        if not run_tools.JOB_ID.fullmatch(job_id) or after < 0:
            return JSONResponse({"detail": "That analysis is not known."}, status_code=404)
        data = await run_in_threadpool(manager().live, job_id, after, held)
        if data is None:
            return JSONResponse({"detail": "That analysis is not known."}, status_code=404)
        return data

    @app.post("/api/runs/{job_id}/open-report")
    async def open_report(job_id: str):
        if not run_tools.JOB_ID.fullmatch(job_id) or await run_in_threadpool(manager().report_file, job_id) is None:
            return JSONResponse({"detail": "That analysis has no saved report to open."}, status_code=404)
        opened = await run_in_threadpool(manager().show_report, job_id)
        return {"opened": bool(opened)}

    @app.get("/api/reports-folder")
    async def reports_folder() -> dict:
        """Where saved reports live and whether that folder exists yet (for the shortcut under the promotion)."""
        return await run_in_threadpool(manager().reports_folder)

    @app.post("/api/reports-folder/open")
    async def open_reports_folder() -> dict:
        """Open the reports folder in the computer's file manager. The page names no path: the server uses its own."""
        return await run_in_threadpool(manager().show_reports_folder)

    @app.post("/api/export")
    async def export_file(body: ExportRequest):
        """A finished analysis as a CSV (one row each, for any number) or a PDF (one analysis). The page saves the bytes."""
        if body.format not in ("csv", "pdf") or not all(run_tools.JOB_ID.fullmatch(i) for i in body.ids):
            return JSONResponse({"detail": "Choose CSV or PDF and finished analyses from the list."}, status_code=422)
        jobs = await run_in_threadpool(manager().finished, body.ids or None)
        if not jobs:
            return JSONResponse({"detail": "There is no finished analysis to save yet."}, status_code=422)
        if body.format == "csv":
            return Response(exports.build_csv(jobs), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="analyses.csv"'})
        if len(jobs) != 1:
            return JSONResponse({"detail": "A PDF is made for one analysis at a time."}, status_code=422)
        page = await run_in_threadpool(manager().report_file, jobs[0].id)
        if page is None:
            return JSONResponse({"detail": "This analysis has no saved HTML report to turn into a PDF."}, status_code=422)
        try:
            data = await run_in_threadpool(exports.html_to_pdf, page)
        except exports.ExportError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        return Response(data, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="report.pdf"'})

    @app.get("/api/picker/options")
    async def picker_options() -> dict:
        return await run_in_threadpool(searcher().options)

    @app.post("/api/picker/run", status_code=202)
    async def picker_run(body: PickerRequest):
        """Start a free stock search (no AI). One runs at a time; the page then polls ``/api/picker/jobs/{id}``."""
        overrides = body.settings.model_dump(exclude_none=True)
        custom_request = body.custom.model_dump() if body.custom is not None else None
        try:
            job = await run_in_threadpool(lambda: searcher().start(body.recipe, body.text, overrides, body.record, custom_request))
        except picker_jobs.SearchBusy as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        return job

    @app.post("/api/picker/count")
    async def picker_count(body: PickerCountRequest):
        """How many stocks Yahoo says match the filters so far: one cheap call, so the page can show the effect of a filter."""
        try:
            return await run_in_threadpool(searcher().count, body.custom.model_dump())
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        except Exception as exc:  # noqa: BLE001 - a plain sentence, never a traceback
            return JSONResponse({"detail": f"Yahoo could not be asked just now ({type(exc).__name__}). Try again in a minute."}, status_code=502)

    @app.get("/api/picker/jobs/{job_id}")
    async def picker_job(job_id: str):
        job = await run_in_threadpool(searcher().get, job_id) if picker_jobs.SEARCH_ID.fullmatch(job_id) else None
        if job is None:
            return JSONResponse({"detail": "That search is not known. Run it again."}, status_code=404)
        return job

    @app.post("/api/picker/export")
    async def picker_export(body: PickerExportRequest):
        """A finished search as a CSV; the page saves the bytes."""
        data = await run_in_threadpool(searcher().csv, body.id) if picker_jobs.SEARCH_ID.fullmatch(body.id) else None
        if data is None:
            return JSONResponse({"detail": "There is no finished search to save. Run it again."}, status_code=404)
        return Response(data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="stocks.csv"'})

    @app.post("/api/picker/scoreboard")
    async def picker_scoreboard():
        """How the recorded picks did against the S&P 500 (a free download of prices)."""
        from tradingagents.picker.quicklook import PickerError

        try:
            return await run_in_threadpool(searcher().scoreboard)
        except picker_jobs.SearchBusy as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        except PickerError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)
        except Exception as exc:  # noqa: BLE001 - the page gets a plain sentence, never a traceback
            return JSONResponse({"detail": f"The scoreboard stopped unexpectedly ({type(exc).__name__}). Try again in a minute."}, status_code=502)

    @app.post("/api/picker/scoreboard/csv")
    async def picker_scoreboard_csv():
        data = await run_in_threadpool(searcher().scoreboard_csv)
        if data is None:
            return JSONResponse({"detail": "Work out the scoreboard first, then save it."}, status_code=404)
        return Response(data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="scoreboard.csv"'})

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


def find_free_port(start: int = DEFAULT_PORT, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, port)) != 0:      # nothing is listening there
                return port
    raise OSError(f"No free port from {start} to {start + tries - 1}.")


def running_here(port: int, timeout: float = 1.0) -> bool:
    """True when this program's page already answers on this computer at ``port`` (so a second copy is not started)."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/", timeout=timeout) as answer:     # nosec: loopback only
            page = answer.read(20000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - nothing there, something else there, or too slow: all mean "not ours"
        return False
    return 'name="council-token"' in page and "Analyst Consortium" in page


def run_server(port: int | None = None, open_browser: bool = True, dotenv_path: Path | None = None) -> None:
    import uvicorn

    wanted = port or DEFAULT_PORT
    if running_here(wanted):
        # Already open (for example the shortcut was double-clicked twice): show that one. A second copy on another port would
        # also start with its own, empty display choices, which are kept per address.
        url = f"http://{HOST}:{wanted}/"
        print(f"Analyst Consortium is already running: {url}")
        if open_browser:
            webbrowser.open(url)
        return
    port = find_free_port(wanted)
    app = create_app(secrets.token_urlsafe(24), dotenv_path, port)
    url = f"http://{HOST}:{port}/"
    print(f"Analyst Consortium: {url}\nPress Ctrl+C to stop, or just close this window. It answers only on this computer.")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=port, log_level="warning", access_log=False)

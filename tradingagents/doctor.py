"""``tradingagents doctor``: say in plain words what works, what still needs setting up and what is broken (a local addition).

It checks four things and changes nothing:

* **environment**: Python, the folders the app writes to, the .env file, the internet connection and the certificates;
* **AI connections**: for the quick and the deep tier, the provider's key (or sign-in), the model name, and one tiny
  live call (plus a structured-answer test) with the time it took;
* **data sources**: every source the analysts draw on, whether it needs a key and whether it answers right now;
* **settings**: the switches that change what a run does and what it costs.

Every result has a status in words (OK, NEEDS SETUP, NOT WORKING, OFF, SKIPPED), a sentence of detail and, when
something is wrong, what to do about it. Keys are never printed: a key is reported as set or not set, and every
message is scrubbed of anything that looks like a secret before it is shown. The same engine serves the command line
(``tradingagents doctor``, with ``--json`` for programs) and, later, a setup screen.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

import tradingagents

OK, NEEDS_SETUP, NOT_WORKING, OFF, SKIPPED = "ok", "needs_setup", "not_working", "off", "skipped"
LABEL = {OK: "OK", NEEDS_SETUP: "NEEDS SETUP", NOT_WORKING: "NOT WORKING", OFF: "OFF", SKIPPED: "SKIPPED"}
GROUPS = ("environment", "ai", "data", "settings")
GROUP_TITLES = {"environment": "Your computer and folders", "ai": "AI connections",
                "data": "Data sources", "settings": "Your settings"}
CERT_ENV = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE")
MIN_PYTHON = (3, 11)

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD)", re.IGNORECASE)
_EXAMPLE_VALUE = re.compile(r"(?i)^(your[_ -].*|placeholder|changeme|change[_-]me|x{3,}|<.*>|\.\.\.)$")


# --------------------------------------------------------------------------------------------------- the pieces

@dataclass
class Result:
    id: str
    group: str
    title: str
    status: str
    detail: str
    fix: str = ""
    enables: str = ""           # what the part adds, for the optional ones
    required: bool = False
    seconds: float | None = None


@dataclass
class Reply:
    status: int
    body: str = ""


class CertificateProblem(Exception):
    """A secure connection failed its certificate check (often an antivirus that re-signs web traffic)."""


class NetworkProblem(Exception):
    """The request did not get an answer: ``kind`` says why in a few words."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def real_http_get(url: str, headers: Mapping[str, str] | None = None, params: Mapping[str, str] | None = None,
                  timeout: float = 12, max_bytes: int = 4000) -> Reply:
    """GET one URL and read at most ``max_bytes`` of the answer. Never puts the URL (which may hold a key) in an error."""
    import requests

    try:
        with requests.get(url, headers=dict(headers or {}), params=dict(params or {}), timeout=timeout, stream=True) as r:
            chunk = next(r.iter_content(chunk_size=max_bytes), b"")
            return Reply(r.status_code, chunk.decode("utf-8", errors="replace"))
    except requests.exceptions.SSLError:
        raise CertificateProblem() from None
    except requests.exceptions.Timeout:
        raise NetworkProblem("the request timed out") from None
    except requests.exceptions.ConnectionError:
        raise NetworkProblem("no connection") from None
    except requests.exceptions.RequestException as exc:
        raise NetworkProblem(type(exc).__name__) from None


@dataclass
class Context:
    env: Mapping[str, str]
    config: dict
    http_get: Callable[..., Reply] = real_http_get
    network: bool = True            # False: skip every check that needs the internet
    live_ai: bool = True            # False: skip the live calls to the AI providers
    dotenv_path: str | None = None
    make_llm: Callable[[str], Any] | None = None     # tier -> chat model; default: the app's own client
    yahoo_rows: Callable[[], int] | None = None      # rows of recent SPY prices; default: yfinance
    now: Callable[[], datetime] = datetime.now

    def llm(self, tier: str):
        if self.make_llm is not None:
            return self.make_llm(tier)
        from tradingagents.llm_clients import create_tier_client
        return create_tier_client(self.config, tier).get_llm()


@dataclass
class Report:
    results: list[Result]
    generated: str
    counts: dict[str, int] = field(default_factory=dict)
    ready: bool = True
    skipped_some: bool = False

    def to_dict(self) -> dict:
        return {"generated": self.generated, "ready": self.ready, "counts": self.counts,
                "skipped_some": self.skipped_some, "results": [asdict(r) for r in self.results]}


# --------------------------------------------------------------------------------------------------- small helpers

def scrub(text: Any, env: Mapping[str, str] | None = None, limit: int = 300) -> str:
    """The text with anything that looks like a secret replaced, on one line and cut to ``limit`` characters."""
    out = str(text)
    for name, value in (env or {}).items():
        if _SECRET_NAME.search(name) and value and len(value) >= 8:
            out = out.replace(value, "[hidden]")
    out = re.sub(r"(?i)(api[_-]?key|apikey|token|access_token|secret)=([^&\s'\"]+)", r"\1=[hidden]", out)
    out = re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}", "[hidden]", out)
    out = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._\-]{10,}", "Bearer [hidden]", out)
    return out.replace("\n", " ")[:limit]


def key_is_set(env: Mapping[str, str], name: str) -> bool:
    value = (env.get(name) or "").strip()
    return bool(value) and not _EXAMPLE_VALUE.match(value)


def _out(status: str, detail: str, fix: str = "") -> tuple[str, str, str]:
    return status, detail, fix


def _skipped(why: str) -> tuple[str, str, str]:
    return _out(SKIPPED, why)


def _network_failure(exc: Exception, what: str) -> tuple[str, str, str]:
    if isinstance(exc, CertificateProblem):
        return _out(NOT_WORKING, f"{what}: the secure connection failed its certificate check.", CERT_FIX)
    return _out(NOT_WORKING, f"{what}: {exc.kind if isinstance(exc, NetworkProblem) else type(exc).__name__}.",
                "Check your internet connection, then run the doctor again.")


CERT_FIX = ("Some antivirus programs (Norton, for example) re-sign secure web traffic, which makes certificate checks "
            "fail. Save your antivirus's root certificate together with the standard bundle in one .pem file and set "
            "REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE and SSL_CERT_FILE to that file in your .env. Pausing HTTPS scanning "
            "in the antivirus also works.")


# --------------------------------------------------------------------------------------------------- environment

def check_python(ctx: Context):
    v = sys.version_info
    text = f"Python {v.major}.{v.minor}.{v.micro} on {platform.system()} {platform.release()}; tradingagents {tradingagents.__version__}"
    if (v.major, v.minor) < MIN_PYTHON:
        return _out(NOT_WORKING, text, f"TradingAgents needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.")
    return _out(OK, text)


def check_folders(ctx: Context):
    cfg = ctx.config
    wanted = {"results": cfg.get("results_dir"), "cache": cfg.get("data_cache_dir"),
              "memory log": os.path.dirname(cfg.get("memory_log_path") or "") or None}
    bad, free = [], None
    for role, path in wanted.items():
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path, delete=True):
                pass
            if free is None:
                free = shutil.disk_usage(path).free / 1e9
        except OSError as exc:
            bad.append(f"{role} ({path}): {type(exc).__name__}")
    if bad:
        return _out(NOT_WORKING, "Cannot write to: " + "; ".join(bad),
                    "Pick folders you can write to with TRADINGAGENTS_RESULTS_DIR, TRADINGAGENTS_CACHE_DIR and "
                    "TRADINGAGENTS_MEMORY_LOG_PATH in your .env.")
    if free is not None and free < 1.0:
        return _out(NEEDS_SETUP, f"The results, cache and memory-log folders can be written, but only {free:.1f} GB is free.",
                    "Runs save reports and logs; free some disk space.")
    return _out(OK, "The results, cache and memory-log folders can be written"
                + (f" ({free:.0f} GB free)." if free is not None else "."))


def check_dotenv(ctx: Context):
    if ctx.dotenv_path:
        return _out(OK, "A .env file was found (its contents are not read or shown here).")
    from tradingagents.llm_clients.api_key_env import get_api_key_env
    names = {get_api_key_env(_tier_info(ctx, tier)[0]) for tier in ("quick", "deep")} - {None}
    if names and all(key_is_set(ctx.env, n) for n in names):
        return _out(OK, "No .env file was found, but the keys are set as environment variables, which works.")
    return _out(NEEDS_SETUP, "No .env file was found in this folder or above it.",
                "Copy .env.example to .env and put your keys there, or set them as environment variables.")


def check_network(ctx: Context):
    if not ctx.network:
        return _skipped("Offline mode: the internet was not tested.")
    try:
        reply = ctx.http_get("https://api.stlouisfed.org/fred/")
    except (CertificateProblem, NetworkProblem) as exc:
        return _network_failure(exc, "Could not reach the internet")
    return _out(OK, f"The internet answers and certificates verify (test answer: HTTP {reply.status}).")


def check_certificates(ctx: Context):
    set_vars = [name for name in CERT_ENV if (ctx.env.get(name) or "").strip()]
    if not set_vars:
        return _out(OFF, "No custom certificate bundle is set; the system's certificates are used.")
    missing = [name for name in set_vars if not os.path.isfile(ctx.env[name].strip())]
    if missing:
        return _out(NOT_WORKING, f"{', '.join(missing)} points to a file that does not exist.",
                    "Fix the path in your .env, or remove the setting if you do not need a custom bundle.")
    return _out(OK, f"A custom certificate bundle is set ({', '.join(set_vars)}) and the file exists.")


# --------------------------------------------------------------------------------------------------- AI connections

PING_PROMPT = "Reply with the single word OK."
STRUCTURED_PROMPT = "Call the tool with ok set to true."


def _rejects_temperature(provider: str, model: str) -> bool:
    return provider == "anthropic" and bool(re.match(r"(?i)claude-(sonnet|haiku|opus|fable|mythos)-5", model or ""))


def _error_status(exc: Exception) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        status = getattr(obj, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def explain_ai_error(exc: Exception, env: Mapping[str, str]) -> str:
    """What a failed provider call most likely means, in a sentence, without echoing anything secret."""
    status, name, text = _error_status(exc), type(exc).__name__.lower(), str(exc).lower()
    if "certificate" in text or "ssl" in name:
        return "the secure connection failed its certificate check (see the certificates note)."
    if status in (401, 403) or "authentication" in name or "invalid x-api-key" in text or ("api key" in text and "invalid" in text):
        return "the provider refused the key; check it in your .env."
    if status == 404 or "notfound" in name or "model_not_found" in text:
        return "the provider does not know this model name; check the model setting."
    if status == 400 and "temperature" in text:
        return "this model rejects a temperature setting; remove TRADINGAGENTS_TEMPERATURE."
    if status == 429 or "ratelimit" in name or "rate limit" in text or "quota" in text:
        return "the provider's rate limit or quota was reached; wait a moment or check your plan and billing."
    if "timeout" in name or "timed out" in text:
        return "the provider did not answer in time."
    return f"{type(exc).__name__}: {scrub(exc, env)}"


def _tier_info(ctx: Context, tier: str) -> tuple[str, str]:
    from tradingagents.llm_clients import tier_provider
    return tier_provider(ctx.config, tier), str(ctx.config.get(f"{tier}_think_llm") or "")


def make_provider_key_check(provider: str):
    def check(ctx: Context):
        from tradingagents.llm_clients.api_key_env import get_api_key_env
        from tradingagents.llm_clients.openai_client import SIGNED_IN_PROVIDERS

        if provider in SIGNED_IN_PROVIDERS:
            from tradingagents.llm_clients import chatgpt_auth
            who = chatgpt_auth.signed_in_as()
            if who:
                return _out(OK, f"Signed in to {provider} (a sign-in, not a key).")
            return _out(NEEDS_SETUP, f"Not signed in to {provider}.", f"Run: tradingagents login {provider}")
        name = get_api_key_env(provider)
        if name is None:
            return _out(OK, f"{provider} needs no key.")
        if key_is_set(ctx.env, name):
            return _out(OK, f"{name} is set (the value is not shown).")
        if (ctx.env.get(name) or "").strip():
            return _out(NEEDS_SETUP, f"{name} still holds the example text.", f"Put your real key in {name} in your .env.")
        return _out(NEEDS_SETUP, f"{name} is not set.", f"Add {name}=<your key> to your .env file, then run the doctor again.")
    return check


def make_model_check(tier: str):
    def check(ctx: Context):
        from tradingagents.llm_clients.validators import validate_model
        provider, model = _tier_info(ctx, tier)
        if not model:
            return _out(NEEDS_SETUP, f"No {tier} model is chosen.", f"Set TRADINGAGENTS_{tier.upper()}_THINK_LLM in your .env.")
        text = f"{provider}, model {model}"
        temp = ctx.config.get("temperature")
        if temp not in (None, "") and _rejects_temperature(provider, model):
            return _out(NOT_WORKING, f"{text}: this model rejects a temperature setting, and one is set ({temp}).",
                        "Remove TRADINGAGENTS_TEMPERATURE from your .env.")
        if not validate_model(provider, model):
            # A note, not a blocker: new models appear before the app's list is updated. The live test decides.
            return _out(OK, f"{text} (note: this name is not in the app's list for {provider}; the live test shows whether it works).")
        return _out(OK, text + ".")
    return check


def make_live_check(tier: str):
    def check(ctx: Context):
        provider, model = _tier_info(ctx, tier)
        if not ctx.live_ai:
            return _skipped("Skipped: no live call to the AI provider was made (it costs a fraction of a cent).")
        from tradingagents.llm_clients.api_key_env import get_api_key_env
        key = get_api_key_env(provider)
        if key and not key_is_set(ctx.env, key):
            return _skipped(f"Skipped: {key} is not set.")
        started = time.time()
        try:
            reply = ctx.llm(tier).invoke(PING_PROMPT)
        except Exception as exc:   # a provider can fail in many ways; each is explained, none is fatal
            return _out(NOT_WORKING, f"The {tier} model ({model}) did not answer: {explain_ai_error(exc, ctx.env)}")
        seconds = time.time() - started
        usage = getattr(reply, "usage_metadata", None) or {}
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        return _out(OK, f"The {tier} model ({model}) answered in {seconds:.1f} s"
                    + (f" using {tokens} tokens." if tokens else "."))
    return check


def make_structured_check(tier: str):
    def check(ctx: Context):
        if not ctx.live_ai:
            return _skipped("Skipped: no live call to the AI provider was made.")
        from pydantic import BaseModel

        from tradingagents.agents.structured import bind_structured
        from tradingagents.llm_clients.api_key_env import get_api_key_env
        provider, model = _tier_info(ctx, tier)
        key = get_api_key_env(provider)
        if key and not key_is_set(ctx.env, key):
            return _skipped(f"Skipped: {key} is not set.")

        class Ping(BaseModel):
            ok: bool

        try:
            llm = ctx.llm(tier)
            bound = bind_structured(llm, Ping, "doctor")
            if bound is None:
                return _out(NEEDS_SETUP, f"The {tier} model ({model}) cannot return structured answers; the agents "
                            "will fall back to plain text, which is slower and less reliable.",
                            "Use a model that supports structured output if you can.")
            answer = bound.invoke(STRUCTURED_PROMPT)
        except Exception as exc:
            return _out(NOT_WORKING, f"A structured answer from the {tier} model failed: {explain_ai_error(exc, ctx.env)}",
                        "Agents retry once and then fall back to plain text, so runs still work, but check the model.")
        if getattr(answer, "ok", None) is True:
            return _out(OK, f"The {tier} model returns structured answers.")
        return _out(NEEDS_SETUP, f"The {tier} model answered, but not in the structured form that was asked for.",
                    "Agents will retry and fall back to plain text; consider another model.")
    return check


# --------------------------------------------------------------------------------------------------- data sources

def _need_net(ctx: Context):
    return None if ctx.network else _skipped("Offline mode: the internet was not tested.")


def check_yahoo(ctx: Context):
    if (skip := _need_net(ctx)):
        return skip
    try:
        if ctx.yahoo_rows is not None:
            rows = ctx.yahoo_rows()
        else:
            from tradingagents.dataflows.vendors.yahoo.health import recent_price_rows
            rows = recent_price_rows("SPY")
    except Exception as exc:
        return _out(NOT_WORKING, f"Yahoo Finance did not answer: {type(exc).__name__}.",
                    "Yahoo is the main source of prices and fundamentals. Check the internet and certificates above, "
                    "and update yfinance (pip install -U yfinance) if it persists.")
    if rows > 0:
        return _out(OK, f"Yahoo Finance answers ({rows} recent daily prices for SPY; no key needed).")
    return _out(NOT_WORKING, "Yahoo Finance answered with no prices.", "Try again later, or update yfinance.")


def check_sec(ctx: Context):
    if (skip := _need_net(ctx)):
        return skip
    agent = (ctx.env.get("SEC_EDGAR_USER_AGENT") or "").strip()
    placeholder = not agent
    agent = agent or f"TradingAgents/{tradingagents.__version__} (contact@example.com)"
    try:
        reply = ctx.http_get("https://data.sec.gov/submissions/CIK0000320193.json", headers={"User-Agent": agent})
    except (CertificateProblem, NetworkProblem) as exc:
        return _network_failure(exc, "SEC EDGAR")
    if reply.status == 403:
        return _out(NOT_WORKING, "SEC EDGAR refused the request (HTTP 403): it rejects clients that do not identify themselves.",
                    'Set SEC_EDGAR_USER_AGENT="Your Name your@email.com" in your .env.')
    if reply.status != 200:
        return _out(NOT_WORKING, f"SEC EDGAR answered HTTP {reply.status}.", "Try again later.")
    if placeholder:
        return _out(NEEDS_SETUP, "SEC EDGAR answers, using the project's placeholder contact address.",
                    'The SEC asks every client to identify itself: set SEC_EDGAR_USER_AGENT="Your Name your@email.com" in your .env.')
    return _out(OK, "SEC EDGAR answers (filings and statements as filed; no key, your contact is sent as asked).")


def check_fred(ctx: Context):
    if (skip := _need_net(ctx)):
        return skip
    if not key_is_set(ctx.env, "FRED_API_KEY"):
        return _out(NEEDS_SETUP, "FRED_API_KEY is not set.",
                    "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html and add FRED_API_KEY=<key> to your .env.")
    try:
        reply = ctx.http_get("https://api.stlouisfed.org/fred/series/observations",
                             params={"series_id": "DGS10", "file_type": "json", "limit": "1",
                                     "api_key": ctx.env["FRED_API_KEY"].strip()})
    except (CertificateProblem, NetworkProblem) as exc:
        return _network_failure(exc, "FRED")
    if reply.status == 200:
        return _out(OK, "FRED answers and accepts the key.")
    if reply.status in (400, 401, 403):
        return _out(NOT_WORKING, f"FRED refused the key (HTTP {reply.status}).", "Check FRED_API_KEY in your .env.")
    return _out(NOT_WORKING, f"FRED answered HTTP {reply.status}.", "Try again later.")


def check_finra(ctx: Context):
    if (skip := _need_net(ctx)):
        return skip
    from tradingagents.dataflows.vendors.finra_short import _DAILY_URL, _HEADERS
    day, tried = ctx.now(), 0
    last_exc = None
    while tried < 6:
        day -= timedelta(days=1)
        if day.weekday() >= 5:
            continue
        tried += 1
        try:
            reply = ctx.http_get(_DAILY_URL.format(day=day.strftime("%Y%m%d")), headers=_HEADERS)
        except (CertificateProblem, NetworkProblem) as exc:
            last_exc = exc
            break
        if reply.status == 200:
            return _out(OK, "FINRA short-sale data answers (no key needed).")
    if last_exc is not None:
        return _network_failure(last_exc, "FINRA")
    return _out(NOT_WORKING, "No recent FINRA short-sale file could be fetched.", "Try again later; short-interest sections will be missing meanwhile.")


def check_polymarket(ctx: Context):
    if (skip := _need_net(ctx)):
        return skip
    try:
        reply = ctx.http_get("https://gamma-api.polymarket.com/markets", params={"limit": "1"})
    except (CertificateProblem, NetworkProblem) as exc:
        return _network_failure(exc, "Polymarket")
    if reply.status == 200:
        return _out(OK, "Polymarket answers (no key needed).")
    return _out(NOT_WORKING, f"Polymarket answered HTTP {reply.status}.", "Try again later.")


def check_quiver(ctx: Context):
    if (skip := _need_net(ctx)):
        return skip
    if not key_is_set(ctx.env, "QUIVER_API_KEY"):
        return _out(NEEDS_SETUP, "QUIVER_API_KEY is not set.",
                    "Quiver is a paid service. With a token, add QUIVER_API_KEY=<token> to your .env.")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {ctx.env['QUIVER_API_KEY'].strip()}"}
    base = "https://api.quiverquant.com/beta"
    try:
        # Congressional trades are on every plan, so they test the token; insiders depend on the plan.
        congress = ctx.http_get(f"{base}/historical/congresstrading/AAPL", headers=headers)
        insiders = ctx.http_get(f"{base}/live/insiders", params={"ticker": "AAPL", "page": "1", "page_size": "1"}, headers=headers)
    except (CertificateProblem, NetworkProblem) as exc:
        return _network_failure(exc, "Quiver")
    if congress.status == 401:
        return _out(NOT_WORKING, "Quiver rejected the token (HTTP 401).", "Check QUIVER_API_KEY in your .env.")
    if congress.status not in (200, 404):     # 404 only means no trades were found for the test symbol
        return _out(NOT_WORKING, f"Quiver answered HTTP {congress.status}.", "Try again later.")
    if insiders.status == 200:
        insider_text = "Insider filings: available."
    elif insiders.status == 403:
        insider_text = "Insider filings: not in your plan, so insider data comes from Yahoo instead."
    else:
        insider_text = f"Insider filings: answered HTTP {insiders.status}."
    return _out(OK, f"Quiver accepts the token. Congressional trades: available. {insider_text}")


def check_alpha_vantage(ctx: Context):
    if (skip := _need_net(ctx)):
        return skip
    if not key_is_set(ctx.env, "ALPHA_VANTAGE_API_KEY"):
        return _out(NEEDS_SETUP, "ALPHA_VANTAGE_API_KEY is not set.",
                    "Optional. A free key from https://www.alphavantage.co/support/#api-key adds earnings-call transcripts.")
    try:
        reply = ctx.http_get("https://www.alphavantage.co/query", params={
            "function": "GLOBAL_QUOTE", "symbol": "IBM", "apikey": ctx.env["ALPHA_VANTAGE_API_KEY"].strip()})
    except (CertificateProblem, NetworkProblem) as exc:
        return _network_failure(exc, "Alpha Vantage")
    body = reply.body.lower()
    if reply.status == 200 and "invalid api" in body:
        return _out(NOT_WORKING, "Alpha Vantage rejected the key.", "Check ALPHA_VANTAGE_API_KEY in your .env.")
    if reply.status == 200 and ("rate limit" in body or "premium" in body or "call frequency" in body):
        return _out(NEEDS_SETUP, "Alpha Vantage accepts the key but its free daily or per-minute limit was reached.",
                    "Try again later; the free plan allows few calls a day.")
    if reply.status == 200:
        return _out(OK, "Alpha Vantage answers and accepts the key.")
    return _out(NOT_WORKING, f"Alpha Vantage answered HTTP {reply.status}.", "Try again later.")


def _simple_probe(name: str, url: str, params: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None):
    def check(ctx: Context):
        if (skip := _need_net(ctx)):
            return skip
        try:
            reply = ctx.http_get(url, params=params, headers=headers)
        except (CertificateProblem, NetworkProblem) as exc:
            return _network_failure(exc, name)
        if reply.status == 200:
            return _out(OK, f"{name} answers (no key needed).")
        return _out(NOT_WORKING, f"{name} answered HTTP {reply.status}.",
                    "It may be blocking automated requests or be down; that part of the report will rely on other sources.")
    return check


def check_ibkr(ctx: Context):
    folder = (ctx.env.get("IBKR_HELPER_DIR") or "").strip()
    if not folder:
        return _out(OFF, "Not used: IBKR_HELPER_DIR is not set (Yahoo supplies the market statistics).")
    if os.path.isdir(folder):
        return _out(OK, "The IBKR helper folder exists (its sign-in is not tested here).")
    return _out(NOT_WORKING, "IBKR_HELPER_DIR points to a folder that does not exist.", "Fix the path in your .env or remove it.")


# --------------------------------------------------------------------------------------------------- settings

COST_NOTE = ("measured on Anthropic Haiku and Sonnet models: about $0.13 for one pass, about $0.40 for five; "
             "other providers differ")


def check_passes(ctx: Context):
    n = int(ctx.config.get("research_passes") or 1)
    if n == 1:
        return _out(OK, "Research passes: 1 (the original single debate). A borderline stock can flip between neighbouring ratings from run to run.")
    note = f"Research passes: {n}. Each run debates {n} times side by side and reports the median and how many agreed; " \
           f"it costs roughly {n} times the debate stage ({COST_NOTE})."
    if n % 2 == 0:
        return _out(NEEDS_SETUP, note, "Use an odd number (3 or 5) so the median is well defined; with an even number the more cautious middle rating wins.")
    return _out(OK, note)


def _switch(on_text: str, off_text: str, env_name: str):
    """A check for an on/off switch that is set to 1/true/yes/on to turn the feature off."""
    def check(ctx: Context):
        flag = (ctx.env.get(env_name) or "").strip().lower() in ("1", "true", "yes", "on")
        return _out(OFF if flag else OK, off_text if flag else on_text)
    return check


def check_disabled_tools(ctx: Context):
    names = [n.strip() for n in (ctx.env.get("TRADINGAGENTS_DISABLED_TOOLS") or "").split(",") if n.strip()]
    if names:
        return _out(OFF, "Data tools switched off for every run: " + ", ".join(names) + ".")
    return _out(OK, "No data tool is switched off.")


# --------------------------------------------------------------------------------------------------- the list of checks

@dataclass
class Check:
    id: str
    group: str
    title: str
    func: Callable[[Context], tuple[str, str, str]]
    required: bool = False
    enables: str = ""


def build_checks(ctx: Context) -> list[Check]:
    checks = [
        Check("env.python", "environment", "Python and the package", check_python, True),
        Check("env.folders", "environment", "Folders the app writes to", check_folders, True),
        Check("env.dotenv", "environment", ".env settings file", check_dotenv),
        Check("env.network", "environment", "Internet and secure connections", check_network, True),
        Check("env.certificates", "environment", "Custom certificate bundle", check_certificates),
    ]
    seen = set()
    for tier in ("quick", "deep"):
        provider, _ = _tier_info(ctx, tier)
        if provider not in seen:
            seen.add(provider)
            checks.append(Check(f"ai.key.{provider}", "ai", f"Key or sign-in for {provider}", make_provider_key_check(provider), True))
    for tier in ("quick", "deep"):
        checks += [Check(f"ai.{tier}.model", "ai", f"{tier.capitalize()} model", make_model_check(tier), True),
                   Check(f"ai.{tier}.call", "ai", f"{tier.capitalize()} model: live test", make_live_check(tier), True),
                   Check(f"ai.{tier}.structured", "ai", f"{tier.capitalize()} model: structured answers", make_structured_check(tier))]
    checks += [
        Check("data.yahoo", "data", "Yahoo Finance (prices, fundamentals, news, options)", check_yahoo, True,
              "the main source of prices, fundamentals, news and option data"),
        Check("data.sec", "data", "SEC EDGAR (filings and statements)", check_sec, False,
              "filings, statements as filed, 8-K events, share counts"),
        Check("data.fred", "data", "FRED (credit spreads and macro backdrop)", check_fred, False,
              "high-yield spreads, financial conditions, release dates"),
        Check("data.finra", "data", "FINRA (short-sale data)", check_finra, False, "short interest and days to cover"),
        Check("data.polymarket", "data", "Polymarket (prediction markets)", check_polymarket, False, "market-implied odds for macro events"),
        Check("data.quiver", "data", "Quiver (congressional trades, insider filings)", check_quiver, False,
              "congressional trades and insider filings"),
        Check("data.alphavantage", "data", "Alpha Vantage (earnings-call transcripts)", check_alpha_vantage, False,
              "earnings-call transcripts"),
        Check("data.usaspending", "data", "USAspending (federal contracts)", _simple_probe(
            "USAspending", "https://api.usaspending.gov/api/v2/references/toptier_agencies/"), False, "federal contract awards"),
        Check("data.clinicaltrials", "data", "ClinicalTrials.gov (clinical studies)", _simple_probe(
            "ClinicalTrials.gov", "https://clinicaltrials.gov/api/v2/studies", {"pageSize": "1"}), False, "clinical study status"),
        Check("data.openfda", "data", "openFDA (drug recalls)", _simple_probe(
            "openFDA", "https://api.fda.gov/drug/enforcement.json", {"limit": "1"}), False, "FDA recalls"),
        Check("data.stocktwits", "data", "StockTwits (social posts)", _stocktwits_probe(), False, "social sentiment"),
        Check("data.reddit", "data", "Reddit (social posts)", _reddit_probe(), False, "social sentiment"),
        Check("data.ibkr", "data", "Interactive Brokers helper", check_ibkr, False, "IBKR option and volume statistics"),
        Check("settings.passes", "settings", "Research passes and cost", check_passes),
        Check("settings.selloff", "settings", "Selloff rules", _switch(
            "Selloff rules are on: weakness the market explains is not held against a stock.",
            "Selloff rules are switched off (TRADINGAGENTS_NO_SELLOFF_RULES).", "TRADINGAGENTS_NO_SELLOFF_RULES")),
        Check("settings.scorecard", "settings", "Shared scorecard", _switch(
            "The shared scorecard is on: the bull, the bear and the manager see the same facts.",
            "The shared scorecard is switched off (TRADINGAGENTS_NO_SCORECARD).", "TRADINGAGENTS_NO_SCORECARD")),
        Check("settings.tools", "settings", "Data tools", check_disabled_tools),
    ]
    return checks


def _stocktwits_probe():
    def check(ctx: Context):
        if (skip := _need_net(ctx)):
            return skip
        from tradingagents.dataflows.vendors import stocktwits
        return _simple_probe("StockTwits", stocktwits._API.format(ticker="AAPL"),
                             headers={"User-Agent": stocktwits._UA, "Accept": "application/json"})(ctx)
    return check


def _reddit_probe():
    def check(ctx: Context):
        if (skip := _need_net(ctx)):
            return skip
        from tradingagents.dataflows.vendors import reddit
        return _simple_probe("Reddit", reddit._RSS.format(sub="stocks", qs="q=AAPL&restrict_sr=1&limit=1"),
                             headers={"User-Agent": reddit._UA})(ctx)
    return check


# --------------------------------------------------------------------------------------------------- running and showing

def _run_one(ctx: Context, check: Check) -> Result:
    started = time.time()
    try:
        status, detail, fix = check.func(ctx)
    except Exception as exc:   # a check that itself breaks is reported, never fatal
        status, detail, fix = NOT_WORKING, f"The check itself failed: {type(exc).__name__}: {scrub(exc, ctx.env)}", ""
    # Whatever a check says is scrubbed of anything that looks like a secret before it can be shown or saved.
    return Result(check.id, check.group, check.title, status, scrub(detail, ctx.env, 600), scrub(fix, ctx.env, 700),
                  check.enables, check.required, round(time.time() - started, 2))


def run_doctor(*, offline: bool = False, live_ai: bool = True, groups: list[str] | None = None,
               ctx: Context | None = None) -> Report:
    """Run the checks and return the report. Pass ``ctx`` to test with fakes."""
    if ctx is None:
        from dotenv import find_dotenv

        from tradingagents.default_config import build_default_config
        ctx = Context(env=dict(os.environ), config=build_default_config(), network=not offline, live_ai=live_ai and not offline,
                      dotenv_path=find_dotenv(usecwd=True) or None)
    checks = [c for c in build_checks(ctx) if not groups or c.group in groups]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda c: _run_one(ctx, c), checks))
    counts = {s: sum(1 for r in results if r.status == s) for s in LABEL}
    ready = not any(r.required and r.status in (NEEDS_SETUP, NOT_WORKING) for r in results)
    return Report(results, datetime.now().strftime("%Y-%m-%d %H:%M"), counts, ready, counts[SKIPPED] > 0)


def format_text(report: Report) -> str:
    """The report as plain text: a status word in brackets at the start of every line, no colour needed."""
    c = report.counts
    lines = [f"TradingAgents doctor  ({report.generated})",
             "Plain-words status of your setup. Nothing is changed and no key is shown.", ""]
    summary = f"{c.get(OK, 0)} OK, {c.get(NEEDS_SETUP, 0)} need setup, {c.get(NOT_WORKING, 0)} not working, " \
              f"{c.get(OFF, 0)} off, {c.get(SKIPPED, 0)} skipped."
    lines.append("SUMMARY: " + summary)
    if report.ready:
        lines.append("READY TO RUN: yes. Everything required works" + (" (some checks were skipped, so this is not a full check)." if report.skipped_some else "."))
    else:
        lines.append("READY TO RUN: no. Fix the lines marked REQUIRED below first.")
    for group in GROUPS:
        rows = [r for r in report.results if r.group == group]
        if not rows:
            continue
        lines += ["", GROUP_TITLES[group].upper()]
        for r in rows:
            tag = f"[{LABEL[r.status]}]"
            kind = "REQUIRED" if r.required and r.status in (NEEDS_SETUP, NOT_WORKING) else ("optional" if not r.required and group == "data" else "")
            title = f"{r.title}" + (f" ({kind})" if kind else "")
            lines.append(f"  {tag:<15} {title}: {r.detail}")
            if r.fix and r.status in (NEEDS_SETUP, NOT_WORKING):
                lines.append(f"  {'':<15} To fix: {r.fix}")
            if r.enables and r.status in (NEEDS_SETUP, OFF, NOT_WORKING):
                lines.append(f"  {'':<15} Adds: {r.enables}.")
    return "\n".join(lines) + "\n"


def format_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)

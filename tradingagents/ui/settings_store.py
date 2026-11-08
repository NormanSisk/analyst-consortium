"""Reading and saving the settings the setup screen edits (a local addition).

Settings live in the .env file, as they always have; this module is the only place the screen writes them. It is built
to be hard to misuse:

* only the names in :func:`registry` can be written, so the screen cannot set ``PATH`` or anything else;
* every value is validated before anything is written, and a bad value writes nothing at all;
* a secret (an API key or token) is never returned: the screen only learns whether one is set;
* the file is created owner-only, and a saved value is also put into the running process's environment so the
  next check sees it at once.
"""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, set_key, unset_key

from tradingagents.agents.style import STYLES
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,99}$")
CONTACT = re.compile(r"^[\x20-\x7E]{3,200}$")
SECRET = re.compile(r"^[\x21-\x7E]{8,512}$")           # printable ASCII, no spaces, no line breaks


@dataclass(frozen=True)
class Setting:
    name: str
    kind: str                 # choice, int, model, contact, secret, switch
    label: str
    choices: tuple[str, ...] = ()
    low: int = 0
    high: int = 0


def providers() -> list[str]:
    return sorted(PROVIDER_API_KEY_ENV)


def registry() -> dict[str, Setting]:
    """Every setting the screen may write, by environment-variable name."""
    items = [
        Setting("TRADINGAGENTS_LLM_PROVIDER", "choice", "AI provider", tuple(providers())),
        Setting("TRADINGAGENTS_QUICK_THINK_LLM", "model", "Quick model"),
        Setting("TRADINGAGENTS_DEEP_THINK_LLM", "model", "Deep model"),
        Setting("TRADINGAGENTS_RESEARCH_PASSES", "int", "Research passes", low=1, high=15),
        Setting("TRADINGAGENTS_INVESTING_STYLE", "choice", "Investing style", tuple(STYLES)),
        Setting("SEC_EDGAR_USER_AGENT", "contact", "SEC contact (name and email)"),
        Setting("TRADINGAGENTS_NO_SELLOFF_RULES", "switch", "Selloff rules switched off"),
        Setting("TRADINGAGENTS_NO_SCORECARD", "switch", "Shared scorecard switched off"),
        Setting("FRED_API_KEY", "secret", "FRED key"),
        Setting("QUIVER_API_KEY", "secret", "Quiver token"),
        Setting("ALPHA_VANTAGE_API_KEY", "secret", "Alpha Vantage key"),
    ]
    for provider, env in PROVIDER_API_KEY_ENV.items():
        if env:
            items.append(Setting(env, "secret", f"{provider} key"))
    return {s.name: s for s in items}


def validate(setting: Setting, value: str) -> str:
    """The value ready to store, or ValueError with a sentence that says what is wrong (never the value itself)."""
    if not isinstance(value, str):
        raise ValueError(f"{setting.label}: a text value is needed.")
    text = value.strip()
    if setting.kind == "choice":
        if text.lower() not in setting.choices:
            raise ValueError(f"{setting.label}: choose one of the listed options.")
        return text.lower()
    if setting.kind == "int":
        if not re.fullmatch(r"\d{1,3}", text) or not setting.low <= int(text) <= setting.high:
            raise ValueError(f"{setting.label}: a whole number from {setting.low} to {setting.high}.")
        return str(int(text))
    if setting.kind == "model":
        if not MODEL_NAME.fullmatch(text):
            raise ValueError(f"{setting.label}: letters, digits and . _ : / - only, up to 100 characters.")
        return text
    if setting.kind == "contact":
        if not CONTACT.fullmatch(text):
            raise ValueError(f"{setting.label}: 3 to 200 plain characters on one line, for example Jane Doe jane@example.org.")
        return text
    if setting.kind == "secret":
        if not SECRET.fullmatch(text):
            raise ValueError(f"{setting.label}: 8 to 512 characters with no spaces or line breaks.")
        return text
    if setting.kind == "switch":
        if text != "1":
            raise ValueError(f"{setting.label}: a switch is set to 1 or removed.")
        return "1"
    raise ValueError(f"{setting.label}: unknown kind of setting.")


def locate_dotenv() -> Path:
    """The .env the app already loads, else the one that will be created in the current folder."""
    found = find_dotenv(usecwd=True)
    return Path(found) if found else Path.cwd() / ".env"


def current(env: MutableMapping[str, str] | None = None) -> dict:
    """What the screen may show: plain values for ordinary settings, only set/not-set for secrets."""
    env = os.environ if env is None else env
    values, secrets = {}, {}
    for name, setting in registry().items():
        raw = (env.get(name) or "").strip()
        if setting.kind == "secret":
            secrets[name] = bool(raw)
        elif raw:
            values[name] = raw
    return {"values": values, "secrets": secrets}


def save(updates: dict[str, str | None], dotenv_path: Path | str | None = None,
         env: MutableMapping[str, str] | None = None) -> dict:
    """Validate every update, then write them all. A value of None removes the setting.

    Returns ``{"changed": [names], "path": str}``; no value is ever included. Raises ValueError, writing nothing, when
    any name is not editable or any value is not valid.
    """
    env = os.environ if env is None else env
    known = registry()
    clean: dict[str, str | None] = {}
    for name, value in updates.items():
        if name not in known:
            raise ValueError(f"{name} is not a setting the screen can change.")
        clean[name] = None if value is None else validate(known[name], value)
    if not clean:
        return {"changed": [], "path": str(dotenv_path or locate_dotenv())}
    path = Path(dotenv_path) if dotenv_path else locate_dotenv()
    if not path.exists():
        # The file holds credentials: create it readable by its owner only.
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass                      # not every file system keeps modes (Windows): best effort
    for name, value in clean.items():
        if value is None:
            unset_key(str(path), name)
            env.pop(name, None)
        else:
            set_key(str(path), name, value)
            env[name] = value
    return {"changed": sorted(clean), "path": str(path)}

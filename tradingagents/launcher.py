"""Starting the app with a double-click (a local addition).

``tradingagents shortcut`` puts a launcher on your desktop (or in the Start menu, or in a folder you name). Double-clicking it runs the
same ``tradingagents ui`` you would type, from the folder that holds your ``.env`` and your data, so your keys, settings and results are
where they always were. It changes no program files, so you can still edit the program and see the change the next time it starts.

Windows gets a real shortcut with the app's icon; macOS gets a ``.command`` file; Linux gets a ``.desktop`` entry. The macOS and Linux
files are written from those systems' documentation and are tested here only as text.
"""

from __future__ import annotations

import importlib.util
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

NAME = "Analyst Consortium"
DESCRIPTION = "Open Analyst Consortium in your browser (it runs on this computer only)"
WHERES = ("desktop", "start-menu", "here")
LINK_NAMES = {"windows": f"{NAME}.lnk", "mac": f"{NAME}.command", "linux": f"{NAME}.desktop"}
CMD_UNSAFE = set('%^&|<>!"')                     # characters cmd.exe would act on inside a path


class ShortcutError(RuntimeError):
    """The shortcut could not be made or removed; the message says why in plain words."""


@dataclass
class Made:
    path: Path
    start: Path                                  # the folder it starts in: the one holding .env
    command: list[str]
    notes: list[str] = field(default_factory=list)


def system_name(platform: str | None = None) -> str:
    platform = platform or sys.platform
    return "windows" if platform.startswith("win") else "mac" if platform == "darwin" else "linux"


def program(python: str | None = None) -> list[str]:
    """The command that starts the page: the ``tradingagents`` program beside this Python, else this Python running the command module."""
    exe = Path(python or sys.executable)
    for name in ("tradingagents.exe", "tradingagents"):
        if (exe.parent / name).is_file():
            return [str(exe.parent / name), "ui"]
    return [str(exe), "-m", "cli.main", "ui"]


def start_folder(cwd: Path | None = None) -> tuple[Path, bool]:
    """(the folder holding the .env the program already uses, True) or (the current folder, False). The program finds its .env by
    searching up from where it starts, so the launcher must start in the same place."""
    cwd = Path(cwd or Path.cwd())
    for folder in (cwd, *cwd.parents):                      # the same search the program makes: this folder, then each one above
        if (folder / ".env").is_file():
            return folder, True
    return cwd, False


def icon_file(kind: str) -> Path:
    return Path(str(files("tradingagents") / "assets" / f"app-icon.{kind}"))


# ---------------------------------------------------------------- Windows
def ps_quote(text: str) -> str:
    """A PowerShell single-quoted string: a quote inside is doubled and nothing else is special."""
    return "'" + str(text).replace("'", "''") + "'"


def windows_arguments(command: list[str]) -> str:
    """The cmd.exe arguments for the shortcut: name the window, start the program, and wait for a key only if it fails (so an error
    can be read instead of the window vanishing). ``/s`` makes cmd strip exactly the outer quotes."""
    for part in command:
        if CMD_UNSAFE & set(part):
            raise ShortcutError(f"The path {part!r} holds a character (one of % ^ & | < > ! \") that a Windows shortcut cannot carry. "
                                "Move the project to a folder without it, or start the program by typing: tradingagents ui")
    joined = " ".join(f'"{part}"' for part in command)                       # every part quoted: spaces, commas and brackets all survive
    return f'/s /c "title {NAME} & {joined} || pause"'


def windows_script(link: Path, command: list[str], start: Path, icon: Path) -> str:
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        f"$s = $shell.CreateShortcut({ps_quote(link)})",
        "$s.TargetPath = $env:ComSpec",
        f"$s.Arguments = {ps_quote(windows_arguments(command))}",
        f"$s.WorkingDirectory = {ps_quote(start)}",
        f"$s.IconLocation = {ps_quote(icon)}",
        f"$s.Description = {ps_quote(DESCRIPTION)}",
        "$s.Save()",
        "Write-Output 'made'",
    ])


def _powershell(script: str, run: Callable) -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise ShortcutError("PowerShell was not found, and Windows needs it to make a shortcut.")
    done = run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
               capture_output=True, text=True, timeout=60, check=False)
    if done.returncode != 0:
        raise ShortcutError("Windows would not make the shortcut: " + (done.stderr or done.stdout or "no reason given").strip()[:300])
    return (done.stdout or "").strip()


def windows_folder(where: str, run: Callable) -> Path:
    special = {"desktop": "Desktop", "start-menu": "Programs"}[where]
    out = _powershell(f"[Environment]::GetFolderPath('{special}')", run)
    if not out:
        raise ShortcutError(f"Windows did not say where your {where.replace('-', ' ')} folder is.")
    return Path(out.splitlines()[-1])


# ---------------------------------------------------------------- macOS and Linux
def mac_script(command: list[str], start: Path) -> str:
    return "\n".join([
        "#!/bin/bash",
        f"# Opens {NAME}. Close this window to stop it.",
        f"cd {shlex.quote(str(start))} || exit 1",
        " ".join(shlex.quote(part) for part in command),
        "status=$?",
        'if [ $status -ne 0 ]; then echo; read -n 1 -s -r -p "Something went wrong. Press any key to close."; fi',
        "exit $status",
        "",
    ])


def desktop_quote(arg: str) -> str:
    """One argument of a Desktop Entry ``Exec`` line: double-quoted, with the characters the specification names escaped."""
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    return f'"{escaped}"'


def linux_entry(command: list[str], start: Path, icon: Path) -> str:
    return "\n".join([
        "[Desktop Entry]", "Type=Application", f"Name={NAME}", f"Comment={DESCRIPTION}",
        "Exec=" + " ".join(desktop_quote(part) for part in command),
        f"Path={start}", f"Icon={icon}", "Terminal=true", "Categories=Office;Finance;", "",
    ])


def _unix_folder(where: str, system: str, run: Callable, home: Path) -> Path:
    if where == "desktop":
        if system == "linux":
            try:
                done = run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=10, check=False)
                if done.returncode == 0 and done.stdout.strip():
                    return Path(done.stdout.strip())
            except (OSError, subprocess.SubprocessError):
                pass
        return home / "Desktop"
    return home / ("Applications" if system == "mac" else ".local/share/applications")


# ---------------------------------------------------------------- the two actions
def _target_folder(where: str, folder: Path | None, system: str, run: Callable, home: Path, cwd: Path) -> Path:
    if where not in WHERES:
        raise ShortcutError(f"Choose where from: {', '.join(WHERES)}.")
    if folder is not None:
        return Path(folder)
    if where == "here":
        return cwd
    return windows_folder(where, run) if system == "windows" else _unix_folder(where, system, run, home)


def create(where: str = "desktop", folder: Path | None = None, *, system: str | None = None, run: Callable = subprocess.run,
           home: Path | None = None, cwd: Path | None = None, python: str | None = None) -> Made:
    """Make the launcher and say where; ShortcutError in plain words when it cannot."""
    system, home, cwd = system or system_name(), Path(home or Path.home()), Path(cwd or Path.cwd())
    command, (start, env_found) = program(python), start_folder(cwd)
    target = _target_folder(where, folder, system, run, home, cwd)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ShortcutError(f"Could not use the folder {target}: {exc}") from None
    link = target / LINK_NAMES[system]
    if system == "windows":
        _powershell(windows_script(link, command, start, icon_file("ico")), run)
    else:
        text = mac_script(command, start) if system == "mac" else linux_entry(command, start, icon_file("png"))
        link.write_text(text, encoding="utf-8")
        link.chmod(0o755)
    notes = []
    if not env_found:
        notes.append(f"No .env file was found from {cwd}, so the shortcut starts there. The page makes one the first time you save a key. "
                     "If your keys live elsewhere, run this command again from that folder.")
    if not all(importlib.util.find_spec(name) for name in ("fastapi", "uvicorn")):
        notes.append('The page needs two extra libraries that are not installed yet:  pip install "tradingagents[ui]"')
    return Made(link, start, command, notes)


def remove(where: str = "desktop", folder: Path | None = None, *, system: str | None = None, run: Callable = subprocess.run,
           home: Path | None = None, cwd: Path | None = None) -> Path | None:
    """Delete the launcher this module made (and only that file); the path, or None when there was none."""
    system, home, cwd = system or system_name(), Path(home or Path.home()), Path(cwd or Path.cwd())
    link = _target_folder(where, folder, system, run, home, cwd) / LINK_NAMES[system]
    if not link.is_file():
        return None
    try:
        link.unlink()
    except OSError as exc:
        raise ShortcutError(f"Could not remove {link}: {exc}") from None
    return link

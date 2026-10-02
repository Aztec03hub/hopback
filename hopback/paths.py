"""Which OS we are on, and translating paths between Windows and WSL.

A session recorded on Windows stores Windows paths ("C:\\src\\app"). Read from
WSL, the same directory is /mnt/c/src/app. Every cross-OS conversion lives
here so the adapters never hand-roll it.
"""
import functools
import os
import platform
import re
import subprocess
from pathlib import Path, PureWindowsPath

_DRIVE = re.compile(r"^([A-Za-z]):[\\/]")


@functools.lru_cache(maxsize=None)
def this_host():
    """'wsl', 'win', 'mac' or 'linux'."""
    try:
        if "microsoft" in Path("/proc/version").read_text().lower():
            return "wsl"
    except OSError:
        pass
    return {"Darwin": "mac", "Windows": "win"}.get(platform.system(), "linux")


def clean_win(path):
    r"""Drop the \\?\ long-path prefix some tools record (Codex does)."""
    if path and path.startswith("\\\\?\\"):
        return path[4:]
    return path


def is_win_path(path):
    return bool(path and _DRIVE.match(clean_win(path)))


def to_local(path):
    """A path as this process can open it.

    In WSL a Windows path "C:\\a\\b" becomes "/mnt/c/a/b". Anything else is
    returned unchanged, including None.
    """
    path = clean_win(path)
    if this_host() == "wsl" and is_win_path(path):
        p = PureWindowsPath(path)
        return "/mnt/" + p.drive[0].lower() + "/" + "/".join(p.parts[1:])
    return path


def to_windows(path):
    """/mnt/c/a/b -> C:\\a\\b. Paths outside /mnt/<drive> are returned as-is."""
    m = re.match(r"^/mnt/([a-z])(/.*)?$", path or "")
    if not m:
        return path
    return f"{m[1].upper()}:" + (m[2] or "/").replace("/", "\\")


@functools.lru_cache(maxsize=None)
def windows_home():
    """The Windows user profile directory as seen from WSL, or None.

    Tries the obvious /mnt/c/Users/<same name> first, which costs nothing, and
    only then asks Windows, which costs a process launch.
    """
    if this_host() != "wsl":
        return None
    guess = Path("/mnt/c/Users") / os.environ.get("USER", "")
    if guess.is_dir() and os.environ.get("USER"):
        return guess
    try:
        out = subprocess.run(["cmd.exe", "/c", "echo %USERPROFILE%"], capture_output=True,
                             text=True, timeout=5, cwd="/mnt/c").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    local = to_local(out)
    return Path(local) if local and Path(local).is_dir() else None


@functools.lru_cache(maxsize=None)
def powershell():
    """A PowerShell executable reachable from WSL, preferring pwsh 7."""
    for cand in ("/mnt/c/Program Files/PowerShell/7/pwsh.exe",
                 "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"):
        if Path(cand).is_file():
            return cand
    return None


# PowerShell treats the typographic single quotes as quote characters too, so
# each must be doubled like the ASCII one or a folder named "Bob’s files" could
# end the string early and run what follows.
_PS_QUOTES = re.compile("(['\u2018\u2019\u201a\u201b])")


def ps_quote(s):
    """Quote one argument for PowerShell as a literal single-quoted string."""
    return "'" + _PS_QUOTES.sub(r"\1\1", str(s)) + "'"

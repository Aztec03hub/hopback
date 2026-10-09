"""A Source is one session store: which host it lives on, which harness wrote
it, and where its root directory is. Every row in the picker carries one.

Discovery looks in this machine's home directory and, from WSL, also in the
Windows user profile (/mnt/c/Users/<you>), so sessions from both sides of a
WSL laptop show up together, each labelled with where it came from.
"""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .adapters import ADAPTERS
from .fmt import safe_text, shq
from .paths import is_win_path, powershell, ps_quote, this_host, to_local, windows_home

SAFE_ID = re.compile(r"[\w.:-]+")
SRCW = 11  # width of the SOURCE column, fits "hermes·wsl"
HOSTS = {"wsl": "WSL", "win": "Windows", "linux": "Linux", "mac": "macOS"}


@dataclass(frozen=True)
class Source:
    host: str
    adapter: ModuleType
    root: Path

    def __reduce__(self):
        # A module can't be pickled, its name can; the picker sends rows to a
        # preview process.
        return _source, (self.host, self.adapter.NAME, self.root)

    @property
    def tag(self):
        return f"{self.adapter.NAME}·{self.host}"

    @property
    def label(self):
        return f"{self.adapter.LABEL} on {HOSTS.get(self.host, self.host)}"

    @property
    def foreign(self):
        """True when sessions here belong to another OS than the one running us."""
        return self.host != this_host()

    def launch(self, row, yolo):
        """(argv, directory to run it in, human-readable command).

        A local session runs its harness directly in the session's directory.
        A Windows session seen from WSL must run on Windows, in its Windows
        directory, so it is handed to PowerShell, which also reports clearly
        when the harness is not installed on that side.
        """
        argv = self.adapter.resume_cmd(self.root, row, yolo)
        cwd = row["cwd"]
        # Ids come from files on disk; anything but a plain token is refused
        # rather than passed to a shell or to PowerShell.
        if not SAFE_ID.fullmatch(row["id"]):
            raise RuntimeError(f"refusing to resume an unusual session id: {row['id']!r}")
        # No path or argument holds a NUL, and one cannot be passed to chdir or
        # exec. Other control and invisible characters are real (a directory made
        # by a CRLF script ends in \r) and are resumed as they are; only the
        # printed command (`shown`) renders them as $'\xNN' escapes.
        if any("\x00" in (a or "") for a in (cwd, *argv)):
            raise RuntimeError(f"refusing to resume with a NUL in the directory or command: {cwd!r}")
        if self.host == "win" and this_host() == "wsl":
            ps = powershell()
            if not ps:
                raise RuntimeError("no PowerShell found under /mnt/c to run a Windows session")
            win_cwd = cwd if is_win_path(cwd) else ""
            exe = ps_quote(argv[0])
            script = (f"if (-not (Get-Command {exe} -ErrorAction SilentlyContinue)) "
                      f"{{ Write-Host '{argv[0]} is not installed on Windows (not on PATH).'; "
                      f"exit 127 }}; ")
            if win_cwd:
                # -ErrorAction Stop: if the folder is gone, stop here rather
                # than start the agent somewhere else.
                script += (f"Set-Location -LiteralPath {ps_quote(win_cwd)} "
                           f"-ErrorAction Stop; ")
            script += "& " + " ".join(ps_quote(a) for a in argv)
            local = to_local(win_cwd) if win_cwd else None
            run_in = local if local and Path(local).is_dir() else "/mnt/c"
            shown = safe_text(f"[Windows] {'cd ' + win_cwd + ' && ' if win_cwd else ''}{' '.join(argv)}",
                              line=True)
            return [ps, "-NoLogo", "-NoProfile", "-Command", script], run_in, shown
        shown = " ".join(shq(a) for a in argv)
        if cwd:
            shown = f"cd {shq(cwd)} && {shown}"
        return argv, cwd, shown


def homes():
    """(host, home directory) pairs to search, this machine's first."""
    found = [(this_host(), Path.home())]
    # HOPBACK_WINDOWS_HOME points at a Windows profile directly (tests, demos,
    # unusual mounts); HOPBACK_NO_WINDOWS turns the Windows side off.
    override = os.environ.get("HOPBACK_WINDOWS_HOME")
    if override:
        found.append(("win", Path(override)))
    elif os.environ.get("HOPBACK_NO_WINDOWS") is None:
        win = windows_home()
        if win and win.resolve() != Path.home().resolve():
            found.append(("win", win))
    return found


def discover(harness=None, host=None):
    """Every store found, optionally narrowed to one harness and/or one host."""
    found = []
    for h, home in homes():
        if host and h != host:
            continue
        for adapter in ADAPTERS:
            if harness and adapter.NAME != harness:
                continue
            try:
                found += [Source(h, adapter, root) for root in adapter.roots(home)]
            except OSError:
                continue  # an unreadable home must not take the others down
    return found


def _source(host, adapter_name, root):
    from .adapters import BY_NAME
    return Source(host, BY_NAME[adapter_name], root)

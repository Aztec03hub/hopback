"""A Source is one session store: which host it lives on, which harness wrote
it, and where its root directory is. Every row in the picker carries one.

Phase 1 discovers only the local Claude Code store. Later phases add the
Windows-side stores seen from WSL and the other harness adapters here.
"""
import platform
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .adapters import claude

SRCW = 11  # width of the SOURCE column, fits "hermes·wsl"


def this_host():
    try:
        if "microsoft" in Path("/proc/version").read_text().lower():
            return "wsl"
    except OSError:
        pass
    return {"Darwin": "mac", "Windows": "win"}.get(platform.system(), "linux")


@dataclass(frozen=True)
class Source:
    host: str
    adapter: ModuleType
    root: Path

    @property
    def tag(self):
        return f"{self.adapter.NAME}·{self.host}"


def discover():
    found = []
    if (claude.DEFAULT_ROOT / "projects").is_dir():
        found.append(Source(this_host(), claude, claude.DEFAULT_ROOT))
    return found

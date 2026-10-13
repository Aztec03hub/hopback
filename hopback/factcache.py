"""Persistent per-transcript facts, keyed by path + (size, mtime_ns).

One marshal file under $XDG_CACHE_HOME/hopback, written atomically under flock.
A corrupt, unreadable or old-version file is ignored and rebuilt.
"""
import fcntl
import marshal
import os
from pathlib import Path

VERSION = 1


def _dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "hopback"


def load():
    try:
        data = marshal.loads((_dir() / "facts.bin").read_bytes())
        if data[0] == VERSION and isinstance(data[1], dict):
            return data[1]
    except (OSError, ValueError, EOFError, TypeError, IndexError):
        pass
    return {}


def save(entries, prefix):
    """Merge this store's live entries in; stale keys under prefix drop."""
    d = _dir()
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(d, 0o700)                               # holds paths and session facts
        with open(d / "facts.lock", "w") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return  # another run is writing; skip, next run retries
            merged = {k: v for k, v in load().items() if not k.startswith(prefix)}
            merged.update(entries)
            tmp = d / f"facts.bin.{os.getpid()}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(marshal.dumps((VERSION, merged)))
            os.replace(tmp, d / "facts.bin")
    except OSError:
        pass

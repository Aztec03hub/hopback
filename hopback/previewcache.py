"""Preview details, built off the UI thread and kept between runs.

Reading a session for its preview (prompts, replies, cost) takes tens to
hundreds of milliseconds, far too long to do on every cursor move. So one
background thread builds them: the highlighted row first, then its
neighbours, then every other visible row, while the picker only ever reads
what is already built. Finished details are saved to
~/.cache/hopback/previews.json, keyed by each session's mtime and size, so the
next run starts warm and only re-reads sessions that changed.
"""
import json
import os
import tempfile
import threading
from pathlib import Path

FORMAT = 1           # bump when the shape of details() output changes
NEIGHBOURS = 15      # rows either side of the cursor built before the rest
KEEP = 20000         # entries kept on disk, newest first
TEXT_CAP = 4000      # longest prompt/reply kept; the pane shows at most 700


def cache_path():
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "hopback" / "previews.json"


def _version():
    try:
        from importlib.metadata import version
        return f"{FORMAT}:{version('hopback')}"
    except Exception:  # noqa: BLE001 - running from a checkout, not installed
        return f"{FORMAT}:dev"


def key(row):
    src = row["source"]
    return f"{src.tag}|{src.root}|{row['id']}"


def stamp(row):
    return [row.get("mtime"), row.get("bytes")]


def _trim(d):
    out = dict(d)
    for k in ("prompt", "reply", "opening"):
        v = out.get(k)
        if isinstance(v, str) and len(v) > TEXT_CAP:
            out[k] = v[:TEXT_CAP]
    out["fields"] = [list(f) for f in out.get("fields") or []]
    return out


def build(row):
    """details() for one row, or {"error": text} when it cannot be read."""
    src = row["source"]
    try:
        d = src.adapter.details(src.root, row["id"])
    except Exception as exc:  # noqa: BLE001 - one bad session must not stop the rest
        return {"error": f"could not read this session: {exc.__class__.__name__}: {exc}"}
    if d is None:
        return {"error": "session not found in its store (deleted since the list loaded?)"}
    return _trim(d)


class Previewer:
    def __init__(self, path=None, persist=True):
        self.path = path or cache_path()
        self.persist = persist
        self._mem = {}            # key -> (stamp, details)
        self._dirty = False
        self._cond = threading.Condition()
        self._want = None         # the highlighted row
        self._rows = []           # visible rows, in display order
        self._center = 0
        self._scan = 0            # how far the sweep of _rows has got
        self._on_ready = None
        self._stopped = False
        self._thread = None
        if persist:
            self._load()

    # -- disk ----------------------------------------------------------------
    def _load(self):
        # A damaged or foreign cache is ignored, never fatal: it is rebuilt.
        try:
            data = json.loads(self.path.read_text())
            if data.get("version") != _version():
                return
            mem = {k: (list(st), dict(d)) for k, (st, d) in data["entries"].items()}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return
        self._mem.update(mem)

    def save(self):
        if not self.persist or not self._dirty:
            return
        with self._cond:
            items = list(self._mem.items())
            self._dirty = False
        # Errors are retried next run rather than remembered.
        items = [(k, v) for k, v in items if "error" not in v[1]]
        items.sort(key=lambda kv: kv[1][0][0] or 0, reverse=True)
        data = {"version": _version(), "entries": dict(items[:KEEP])}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".previews.")
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- lookups -------------------------------------------------------------
    def get(self, row):
        """The built details for a row, or None if not built yet. Never blocks."""
        hit = self._mem.get(key(row))
        if hit and hit[0] == stamp(row):
            return hit[1]
        return None

    def get_now(self, row):
        """Build in the caller's thread if needed (the non-interactive path)."""
        d = self.get(row)
        if d is None:
            d = build(row)
            self._put(row, d)
        return d

    def _put(self, row, d):
        with self._cond:
            self._mem[key(row)] = (stamp(row), d)
            self._dirty = True

    # -- background ----------------------------------------------------------
    def start(self, on_ready):
        """on_ready(row) is called from the worker thread after the highlighted
        row's details are built."""
        self._on_ready = on_ready
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hopback-preview",
                                            daemon=True)
            self._thread.start()

    def stop(self):
        with self._cond:
            self._stopped = True
            self._cond.notify()

    def set_rows(self, rows):
        with self._cond:
            self._rows = list(rows)
            self._scan = 0
            self._cond.notify()

    def want(self, row, index):
        with self._cond:
            self._want = row
            self._center = index or 0
            self._cond.notify()

    def _next(self):
        """The next row to build: highlighted, then neighbours, then the rest."""
        w = self._want
        if w is not None and self.get(w) is None:
            return w, True
        rows, c = self._rows, self._center
        for off in range(1, NEIGHBOURS + 1):
            for i in (c + off, c - off):
                if 0 <= i < len(rows) and self.get(rows[i]) is None:
                    return rows[i], False
        while self._scan < len(rows):
            r = rows[self._scan]
            if self.get(r) is None:
                return r, False
            self._scan += 1
        return None, False

    def _run(self):
        while True:
            with self._cond:
                while not self._stopped:
                    row, wanted = self._next()
                    if row is not None:
                        break
                    self._cond.wait()
                if self._stopped:
                    return
            d = build(row)
            self._put(row, d)
            # Tell the UI when the row it is showing is ready; also when the
            # highlight moved onto a row this thread happened to be building.
            if (wanted or self._want is row) and self._on_ready:
                try:
                    self._on_ready(row)
                except Exception:  # noqa: BLE001 - the app may have exited
                    pass

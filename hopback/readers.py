"""File and database readers shared by the adapters."""
import json
import sqlite3
from pathlib import Path


def lines_backwards(path, chunk=1024 * 1024, limit=None):
    """Yield a file's lines (bytes, no newline) from the last to the first.

    Reads fixed-size chunks from the end, so finding the newest record in an
    800 MB transcript costs one chunk, not the whole file. `limit` caps the
    bytes read in total; None reads as far back as needed.
    """
    try:
        fh = Path(path).open("rb")
    except OSError:
        return
    with fh:
        pos = fh.seek(0, 2)
        read = 0
        tail = b""
        while pos > 0 and (limit is None or read < limit):
            start = max(0, pos - chunk)
            fh.seek(start)
            buf = fh.read(pos - start) + tail
            read += pos - start
            pos = start
            lines = buf.split(b"\n")
            # The first piece may be the second half of a line that starts in
            # the previous chunk, so it is carried over unless this is the top.
            tail = lines.pop(0) if pos > 0 else b""
            for line in reversed(lines):
                if line:
                    yield line
        if tail and pos == 0:
            yield tail


def loads(line):
    """json.loads that tolerates the raw control characters some tools write
    into JSONL, and returns None instead of raising on a broken line."""
    try:
        return json.loads(line, strict=False)
    except ValueError:
        return None


def open_ro(db):
    """Open a SQLite database strictly read-only.

    These are live databases other programs are writing (WAL mode), so the
    connection must never take a write lock or create files beside them.
    """
    uri = Path(db).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    return con


def columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}

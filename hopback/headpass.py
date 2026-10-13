"""One read of each transcript head feeds both the entrypoint (adapters/claude)
and the launch facts (launches). Cold starts spread it over a process pool."""
import os

from . import launches

HEAD_LINES = 25


def both(path, deep=None, empty=False):
    """(entrypoint, launch facts), or None when the file is unreadable. The
    entrypoint is None when the single read cannot settle it (a huge first line
    can push it past the head): the caller then reads it the old way."""
    from .adapters import claude
    try:
        with open(path, "rb") as fh:
            head = fh.read(launches.HEAD_BYTES)
    except OSError:
        return None
    lines = head.decode("utf-8", errors="replace").splitlines(keepends=True)[:HEAD_LINES]
    ep, _ = claude.head_lines_facts(lines)
    if not ep:
        ep = None if len(head) == launches.HEAD_BYTES else ""
    return ep, launches.facts_from_head(head), extra(claude, path, ep, deep, empty)


def extra(claude, path, ep, deep, empty):
    """(has_conversation or None, scan or None) for what collect would read
    next; only for the sessions it would not skip on the entrypoint."""
    if deep is None or ep is None or ep.startswith("sdk"):
        return None, None
    try:
        hc = claude.has_conversation(path, path.stat().st_size)
        return hc, (claude.scan(path, whole_file=deep) if hc or empty else None)
    except OSError:
        return None, None


def _work(job):
    batch, deep, empty = job
    return [(str(p), both(p, deep, empty)) for p in batch]


def prefetch(paths, workers=None, deep=None, empty=False):
    """Fill launches.PRE_EP / PRE_FACTS for `paths`. A pool failure falls back
    to this process; an unreadable file is left to the normal path."""
    import concurrent.futures
    import multiprocessing
    if deep is None:
        paths = [p for p in paths if str(p) not in launches.PRE_FACTS
                 and str(p) not in launches.PRE_EP]
    else:
        paths = [p for p in paths if launches.PRE_EXTRA.get(str(p), (None, None)) == (None, None)]
    batches = [(paths[i:i + 64], deep, empty) for i in range(0, len(paths), 64)]
    n = workers or min(8, os.cpu_count() or 1)
    try:
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=n, mp_context=multiprocessing.get_context("spawn")) as pool:
            done = [r for chunk in pool.map(_work, batches) for r in chunk]
    except (OSError, ImportError, NotImplementedError, RuntimeError):
        done = [r for b in batches for r in _work(b)]
    for key, got in done:
        if got is not None:
            if got[0] is not None:
                launches.PRE_EP[key] = got[0]
            if deep is None:
                launches.PRE_FACTS[key] = got[1]
            else:
                launches.PRE_EXTRA[key] = got[2]

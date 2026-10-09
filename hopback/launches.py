"""Which Claude Code sessions were started by another session's Claude.

A script or bot that runs `claude ...` in a shell creates an ordinary
interactive session, indistinguishable on its own from one a person started.
It is told apart by its parent. A session counts as spawned only when
all of these hold:

1. Another session's Claude ran a shell command with `claude` in command
   position (`claude ...`, `env X=1 claude ...`, `tmux new-window "claude ..."`),
   starting a new session rather than resuming one or running a subcommand.
   This is a tool call, which a person's typed or pasted text never is.
2. This session's first record comes at most WINDOW seconds after that call.
3. The start of this session's opening prompt is text that Claude sent it:
   - in the launch command itself, or
   - in a prompt file it wrote (with Write, or a shell command) in the
     LOOKBACK seconds before, when the launch command names that file, or
   - in a `tmux send-keys` (or set/load/paste-buffer) it ran after the launch,
     before the prompt arrived.
   Quote marks and backslashes are ignored on both sides, because the shell
   strips them on the way into the new session.

When copies of one conversation (see `continued-in`) hold the same launch,
the newest copy is named as the parent. Only two genuinely different launches
leave the parent unnamed.

Speed: the first run finds launch candidates with ripgrep when it is installed
(about 2 s over 10 GB) and otherwise with plain Python spread over the CPU
cores.
After that, only bytes appended since the last run are read, because
transcripts are append-only. Results live in $XDG_CACHE_HOME/hopback.
"""
import bisect
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from importlib import metadata
from pathlib import Path

from .shellparse import is_launch

WINDOW = 30.0
LOOKBACK = 600.0          # how far before a launch its prompt file may be written
LOOKBACK_BYTES = 8 * 1024 * 1024
FRAG_MIN, FRAG_MAX = 40, 100
SETTLE = 120.0            # a "not spawned" verdict waits this long to be final
HEAD_BYTES = 256 * 1024

# Cheap byte-level prefilter: a tool call whose command has `claude` as a word
# (after a separator, a path slash, or a JSON-escaped newline or tab; not part
# of `.claude` or `claude-code`). shellparse.is_launch() makes the decision.
_CANDIDATE = re.compile(rb'"tool_use".*"command":"(?:[^\n]*(?:[^\w.-]|\\[nrt]))?claude(?:[^\w-]|$)')
RG_PATTERN = r'"tool_use".*"command":"([^\n]*([^\w.-]|\\[nrt]))?claude([^\w-]|$)'
# tmux commands that put text into a pane: how a prompt is typed in.
_TYPES_TEXT = ("send-keys", "set-buffer", "load-buffer", "paste-buffer")
_FILE = re.compile(r"[\w.-]+\.(?:txt|md|prompt|json)\b")
_TS = re.compile(rb'"timestamp":"([^"]+)"')
_STRIP = re.compile(r"[\"'\\`]")
_SPACE = re.compile(r"\s+")


@functools.cache
def version():
    """The cache is only valid for this exact detection logic, grammar
    included: a new tree-sitter-bash can parse a command differently.
    Computed on first use, not at import: a missing package must surface
    where detection runs (and is shown), not stop every harness loading."""
    return hashlib.sha1(Path(__file__).read_bytes()
                        + (Path(__file__).parent / "shellparse.py").read_bytes()
                        + "|".join(metadata.version(p) for p in ("tree-sitter", "tree-sitter-bash")).encode()
                        ).hexdigest()[:12]


def epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def cache_path(root):
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    tag = hashlib.sha1(str(root).encode()).hexdigest()[:10]
    return Path(base) / "hopback" / f"claude-launches-{tag}.json"


def norm(text):
    """Text as it compares across a shell: no quote marks or backslashes,
    whitespace collapsed."""
    return _SPACE.sub(" ", _STRIP.sub("", text)).strip()


def tool_calls(line):
    """[(tool name, input dict)] of an assistant record's tool calls."""
    try:
        rec = json.loads(line)
    except ValueError:
        return []
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return []
    out = []
    for c in (rec.get("message") or {}).get("content") or []:
        if isinstance(c, dict) and c.get("type") == "tool_use" and isinstance(c.get("input"), dict):
            out.append((c.get("name") or "", c["input"]))
    return out


def launch_command(line):
    """The launching shell command in a transcript line, or None."""
    for _, inp in tool_calls(line):
        cmd = inp.get("command")
        if isinstance(cmd, str) and is_launch(cmd):
            return cmd
    return None


def launch_times(data, base=0):
    """[time, byte offset] of each launch tool call in a chunk of transcript
    bytes that starts at offset `base`."""
    out = []
    pos = 0
    for line in data.split(b"\n"):
        if b'"tool_use"' in line and b"claude" in line and _CANDIDATE.search(line):
            m = _TS.search(line)
            t = epoch(m[1].decode()) if m and b'"type":"assistant"' in line else None
            if t and launch_command(line):
                out.append([t, base + pos])
        pos += len(line) + 1
    return out


def head_facts(path):
    """(time of the first record, the start of the opening prompt, the time
    that prompt arrived). The prompt is "" when there is none worth matching,
    and None when it may not have been written yet. OSError propagates: the
    caller retries the file next time rather than recording nothing."""
    with path.open("rb") as fh:
        head = fh.read(HEAD_BYTES)
    first = frag = when = None
    for line in head.splitlines():
        if first is None:
            m = _TS.search(line)
            if m:
                first = epoch(m[1].decode())
        if b'"type":"user"' in line and frag is None:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            content = (rec.get("message") or {}).get("content") if isinstance(rec, dict) else None
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content
                                   if isinstance(c, dict) and c.get("type") == "text")
            if isinstance(content, str) and content and not content.startswith("<"):
                # The start only: a launch whose quoting truncated the prompt
                # still begins the same way. Too short to be distinctive: "".
                text = norm(content[:600])
                frag = text[:FRAG_MAX] if len(text) >= FRAG_MIN else ""
                when = epoch(rec.get("timestamp", ""))
        if first is not None and frag is not None:
            break
    if frag is None and len(head) == HEAD_BYTES:
        frag = ""                # no prompt in the first 256 KB: never will be
    return first, frag, when


def rg_launches(projects, sizes):
    """{path: (launches, offset read up to)} for the whole store via ripgrep,
    or None without it. Only bytes below each file's size in `sizes` (taken
    before the search) count, cut back to a whole line, so lines appended while
    rg ran are read by the normal incremental pass."""
    rg = shutil.which("rg")
    if not rg:
        return None
    try:
        res = subprocess.run([rg, "--no-ignore", "--null", "-b", "--max-columns", "1",
                              "-g", "*.jsonl", "--max-depth", "2", "-e", RG_PATTERN,
                              str(projects)], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode not in (0, 1):
        return None
    offsets = {}
    for line in res.stdout.splitlines():
        path, _, rest = line.partition(b"\0")
        off = rest.split(b":", 1)[0]
        if off.isdigit():
            offsets.setdefault(os.fsdecode(path), []).append(int(off))
    out = {}
    for key, size in sizes.items():
        try:
            with open(key, "rb") as fh:
                fh.seek(max(0, size - 65536))
                tail = fh.read(size - max(0, size - 65536))
                nl = tail.rfind(b"\n")
                if nl == -1 and size > 65536:
                    continue                 # leave this file to the normal pass
                upto = max(0, size - 65536) + nl + 1
                found = []
                for off in offsets.get(key, ()):
                    if off < upto:
                        fh.seek(off)
                        found += launch_times(fh.readline(), off)
        except OSError:
            continue
        out[key] = (found, upto)
    return out


def _scan_whole(job):
    """Worker for py_launches: (path, size) -> (path, launches, offset)."""
    key, size = job
    try:
        with open(key, "rb") as fh:
            data = fh.read(size)
    except OSError:
        return key, [], 0
    cut = data.rfind(b"\n") + 1
    return key, (launch_times(data[:cut]) if b"claude" in data else []), cut


def py_launches(sizes):
    """rg_launches without ripgrep: the same scan in plain Python, spread over
    the CPU cores, since it reads every byte of the store once."""
    import concurrent.futures
    import multiprocessing
    jobs = sorted(sizes.items(), key=lambda kv: -kv[1])   # big files first
    try:
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=min(8, os.cpu_count() or 1),
                mp_context=multiprocessing.get_context("spawn")) as pool:
            done = list(pool.map(_scan_whole, jobs, chunksize=8))
    except (OSError, ImportError, NotImplementedError, RuntimeError):
        done = [_scan_whole(j) for j in jobs]
    return {key: (found, off) for key, found, off in done}


def _lines_with_times(blob):
    for line in blob.split(b"\n"):
        if b'"tool_use"' not in line:
            continue
        m = _TS.search(line)
        t = epoch(m[1].decode()) if m else None
        if t is not None:
            yield t, line


def prompt_came_from(parent, launch, frag, arrived):
    """Which part of rule 3 (module docstring) shows `frag` is text the
    parent's Claude sent this session: "command", "typed" or "file"; None if
    none does. An unreadable parent raises OSError: "couldn't look" is not
    "no", and the caller must not settle a verdict on it."""
    t0, off = launch
    with open(parent, "rb") as fh:
        fh.seek(off)
        cmd = launch_command(fh.readline())
        if cmd is None:
            return None
        if frag in norm(cmd):
            return "command"
        after = fh.read(LOOKBACK_BYTES) if arrived and arrived > t0 else b""
        files = set(_FILE.findall(cmd))
        before = b""
        if files:
            start = max(0, off - LOOKBACK_BYTES)
            fh.seek(start)
            before = fh.read(off - start)
    # Typed in after the launch, before the prompt arrived.
    for t, line in _lines_with_times(after):
        if t > arrived + 1:
            break
        for _, inp in tool_calls(line):
            c = inp.get("command")
            if (isinstance(c, str) and "tmux" in c and any(k in c for k in _TYPES_TEXT)
                    and frag in norm(c)):
                return "typed"
    # Written to a prompt file shortly before, when the launch names the file.
    if not files:
        return None
    for t, line in reversed(list(_lines_with_times(before))):
        if t < t0 - LOOKBACK:
            break
        for name, inp in tool_calls(line):
            if name == "Write":
                path = str(inp.get("file_path") or "")
                if os.path.basename(path) in files and frag in norm(str(inp.get("content") or "")):
                    return "file"
            elif isinstance(inp.get("command"), str):
                c = inp["command"]
                if any(f in c for f in files) and frag in norm(c):
                    return "file"
    return None


def load_cache(cpath):
    try:
        cache = json.loads(cpath.read_text())
    except (OSError, ValueError):
        cache = None
    if (not isinstance(cache, dict) or cache.get("v") != version()
            or not all(isinstance(cache.get(k), dict) for k in ("files", "verdicts", "evidence"))):
        return {"v": version(), "files": {}, "verdicts": {}, "evidence": {}}
    return cache


def known_parents(root):
    """The verdicts from the last scan, read without scanning: for the preview
    pane, which runs once per cursor move."""
    v = load_cache(cache_path(root))["verdicts"]
    return {sid: p for sid, p in v.items() if isinstance(p, str)}


HOW = {"command": "the prompt is in the launch command",
       "typed": "typed in with tmux after the launch, before the prompt arrived",
       "file": "written to a prompt file the launch names, shortly before it"}
CMD_SHOWN = 400


def _command_at(path, off):
    """The launch command at byte `off` of a transcript, cut to CMD_SHOWN.
    OSError propagates, like prompt_came_from's."""
    with open(path, "rb") as fh:
        fh.seek(off)
        cmd = launch_command(fh.readline()) or ""
    return cmd[:CMD_SHOWN]


def explain(root, sid):
    """[(label, value)] saying why `sid` was marked as spawned: the evidence
    the scan stored with its verdict. [] if it wasn't marked. A damaged entry
    is shown as damaged, never raised."""
    cpath = cache_path(root)
    cache = load_cache(cpath)
    ev = cache["evidence"].get(sid)
    if not isinstance(cache["verdicts"].get(sid), str) or not isinstance(ev, dict):
        return []
    frag = str(ev.get("frag", ""))
    out = [("opening prompt", frag + ("…" if len(frag) >= FRAG_MAX else ""))]
    items = ev.get("launches")
    for item in items if isinstance(items, list) else [None]:
        if not (isinstance(item, list) and len(item) == 5
                and all(isinstance(x, (int, float)) for x in item[1:3])
                and 0 <= item[1] < 4e9                       # a plausible epoch time
                and all(isinstance(x, str) for x in (item[0], *item[3:]))):
            out.append(("evidence", f"damaged in the cache; delete {cpath} to rebuild it"))
            continue
        parent, t, gap, how, cmd = item
        out += [("launched from", f"{parent}  at "
                 + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))),
                ("gap", f"this session began {gap:.1f} s after the launch"),
                ("evidence", HOW.get(how, how)),
                ("launch command", cmd + ("…" if len(cmd) >= CMD_SHOWN else ""))]
    return out


def started_by(root, problems):
    """{session id: parent session id, or "" when several launches qualify}.
    What went wrong without stopping the scan is appended to `problems`."""
    projects = Path(root) / "projects"
    files = {str(p): p for p in projects.glob("*/*.jsonl")}
    cpath = cache_path(root)
    cache = load_cache(cpath)
    known, verdicts = cache["files"], cache["verdicts"]
    changed = False
    for gone in set(known) - set(files):
        del known[gone]
        changed = True

    sizes, unreadable = {}, 0
    for key, path in files.items():
        try:
            sizes[key] = path.stat().st_size
        except OSError:
            unreadable += 1                    # vanished or unreadable: missing from this pass
    cold = [k for k in sizes if k not in known]
    bulk = None
    if len(cold) > 200:
        cold_sizes = {k: sizes[k] for k in cold}
        bulk = rg_launches(projects, cold_sizes)
        if bulk is None:
            bulk = py_launches(cold_sizes)
    for key, size in sizes.items():
        path = files[key]
        ent = known.get(key)
        if ent is None:
            try:
                first, frag, when = head_facts(path)
            except OSError:
                unreadable += 1                # not recorded: read afresh next time
                continue
            ent = known[key] = {"first": first, "frag": frag, "when": when,
                                "off": 0, "launch": []}
            if bulk is not None and key in bulk:
                ent["launch"], ent["off"] = bulk[key]
            changed = True
        if (ent["first"] is None or ent["frag"] is None) and size > ent["off"]:
            # Seen before its opening prompt was written; look again.
            try:
                ent["first"], ent["frag"], ent["when"] = head_facts(path)
            except OSError:
                unreadable += 1
                continue
            changed = True
        if size < ent["off"]:            # rewritten, not appended: start over
            ent["off"], ent["launch"] = 0, []
        if size > ent["off"]:
            try:
                with path.open("rb") as fh:
                    fh.seek(ent["off"])
                    data = fh.read(size - ent["off"])
            except OSError:
                unreadable += 1                # `off` is unchanged: read again next time
                continue
            # Only whole lines; a half-written last line is read next time.
            cut = data.rfind(b"\n") + 1
            if cut:
                ent["launch"] += launch_times(data[:cut], ent["off"])
                ent["off"] += cut
                changed = True

    launches = sorted((t, off, key) for key, ent in known.items() for t, off in ent["launch"])
    times = [t for t, _, _ in launches]
    now = time.time()
    out = {}

    def _mtime(p):                     # a copy deleted mid-scan just can't be the newest
        try:
            return files[p].stat().st_mtime if p in files else 0
        except OSError:
            return 0
    for key, ent in known.items():
        sid = Path(key).stem
        if sid in verdicts:
            if verdicts[sid] is not None:
                out[sid] = verdicts[sid]
            continue
        first, frag = ent["first"], ent["frag"]
        if not first or not frag:
            continue
        lo = bisect.bisect_left(times, first - WINDOW)
        hi = bisect.bisect_right(times, first)
        # Group by launch time: copies of one conversation share a launch.
        by_launch, found = {}, []
        try:
            for t, off, p in launches[lo:hi]:
                how = p != key and prompt_came_from(p, (t, off), frag, ent["when"])
                if how:
                    by_launch.setdefault(t, []).append(p)
                    found.append([Path(p).stem, t, first - t, how, _command_at(p, off)])
        except OSError:
            # A parent transcript couldn't be read (rotated, permissions): no
            # verdict yet, so this is decided again next time, not settled
            # as "not spawned" for good.
            unreadable += 1
            continue
        if len(by_launch) == 1:
            copies = next(iter(by_launch.values()))
            newest = max(copies, key=_mtime)
            verdict = Path(newest).stem
        elif by_launch:
            verdict = ""
        else:
            verdict = None
            # A parent's launch line may not be flushed yet: only a settled
            # session gets a final "not spawned". Nor while any
            # transcript was unreadable this pass: its launches are missing,
            # so "no" can't be known yet.
            if unreadable or now - (ent["when"] or first) < SETTLE:
                continue
        verdicts[sid] = verdict
        changed = True
        if verdict is not None:
            out[sid] = verdict
            cache["evidence"][sid] = {"frag": frag, "launches": found}   # explain() reads it
    if unreadable:
        problems.append(f"{unreadable} session(s) not checked for a spawning launch: a transcript "
                        "could not be read; they are checked again next time")
    if changed:
        tmp = cpath.with_name(f"{cpath.name}.{os.getpid()}.tmp")
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(cache))
            tmp.replace(cpath)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            problems.append(f"could not save the spawn-detection cache, so the next start scans "
                            f"everything again ({exc})")
    return out

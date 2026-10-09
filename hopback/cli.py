"""hopback: browse AI coding-agent sessions across every directory, harness and
OS, then hop back into one.

Each harness (Claude Code, Codex, Hermes) is read by an adapter in
hopback/adapters/, and each store it finds is a Source: (host, harness, root),
see sources.py. From WSL the Windows-side stores are found too, so one list
shows claude·wsl next to claude·win, codex·wsl next to codex·win.

Selecting a session runs that harness's own resume command in the session's
recorded directory. A Windows session picked from WSL is resumed on Windows,
through PowerShell, in its Windows directory.

The UI is Textual rather than fzf. fzf applies its focus colours only where the
row text does not already set its own, so alternating row colours and a uniform
focused row are mutually exclusive there, and it has no mouse-motion events at
all so nothing can highlight on hover. Textual paints the rows itself (a
ScrollView, SessionList, that draws only the visible lines): the stripes, the
uniform focused row and the hover colour are all set in render_line.

Usage:
    hopback                       every session, every harness, every host
    hopback codex                 one harness (claude, codex, hermes)
    hopback --host win            one host (wsl, win, linux, mac)
    hopback codex stremio         harness plus a pre-filter
    hopback -y                    resume skipping permission prompts
    hopback -l                    plain list, no picker (also auto when piped)
    hopback -n 50 / -a            how many to load per store (default 300) / all
    hopback -d                    only sessions from the current directory
    hopback -t / -r / -b          include agent sessions / scheduled runs /
                                  background-job leftovers
    hopback -s / -e / --archived  include /tmp, empty, archived sessions
    hopback --hidden              only the sessions you hid
    hopback --review              open on the Review tab (spawn-detection verdicts)
    hopback --id <prefix>         print the full id for a prefix, then exit
    hopback --print-cd            print the command instead of running it
    hopback --doctor              show every store found and how it resumes

In the picker:
    type to search (space-separated words must all match), arrows to move
    ENTER      resume                 CTRL-Y   resume skipping permissions
    TAB        next harness tab       CTRL-O   cycle hosts
    CTRL-T     agent sessions         CTRL-R   scheduled runs
    CTRL-B     background leftovers   CTRL-X   hide / unhide this session
    CTRL-G     Hidden view            F12      Review view (1 2 3 0 to mark)
    CTRL-/     preview pane           CTRL-U   clear search      ESC quit
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from .adapters import AGENT, BY_NAME, LEFTOVER, ROLES, SCHEDULED, claude
from .fmt import safe_text, size_str, when
from .paths import this_host
from .sources import HOSTS, SRCW, discover

DIRW = 44


def load_rows(sources, limit, here_only, deep, **opts):
    """Every source's rows, tagged with their Source, newest first.

    One broken store (a schema change, a locked database) becomes a warning
    rather than an empty picker: the other stores still load.
    """
    rows, total, warnings = [], 0, []
    for src in sources:
        try:
            # An adapter may add a third item: problems that didn't cost the
            # list (spawn detection failing, say). They are shown, not dropped.
            got, n, *problems = src.adapter.collect(src.root, limit, here_only, deep, **opts)
            warnings += [f"{src.tag}: {p}" for p in (problems[0] if problems else [])]
        except Exception as exc:  # noqa: BLE001 - any adapter failure is isolated
            warnings.append(f"{src.tag}: could not read {src.root} ({exc.__class__.__name__}: {exc})")
            continue
        for r in got:
            r["source"] = src
        rows += got
        total += n
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows, total, warnings


def wrap_into(out, text, width, limit=None):
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    for para in text.splitlines():
        if not para.strip():
            out.append("")
            continue
        line = ""
        for wd in para.split():
            if len(line) + len(wd) + 1 > width:
                out.append("  " + line)
                line = wd
            else:
                line = f"{line} {wd}".strip()
        if line:
            out.append("  " + line)


def show(widget, text):
    """The one way text derived from a session or a store reaches a widget."""
    widget.update(safe_text(text))


def preview(row, width=80):
    """The preview pane for one row, the same shape for every harness."""
    src = row["source"]
    try:
        d = src.adapter.details(src.root, row["id"])
    except Exception as exc:  # noqa: BLE001
        return safe_text(f"could not read this session: {exc.__class__.__name__}: {exc}")
    if d is None:
        return "session not found in its store (deleted since the list loaded?)"
    title = d["title"] if len(d["title"]) <= 200 else d["title"][:199] + "…"
    out = [f"{title}\n", f"  {'source':<16} {src.tag}  ({src.label})"]
    out += [f"  {label:<16} {value}" for label, value in d["fields"]]
    try:
        out.append(f"  {'resume':<16} {src.launch(row, False)[2]}")
    except RuntimeError as exc:
        out.append(f"  {'resume':<16} unavailable: {exc}")
    out.append(f"  {'id':<16} {row['id']}\n")
    if row.get("review"):
        from . import launches
        out.append("▸ why it was marked as spawned\n")
        verdict = row.get("review_verdict")
        if verdict == "rejected":
            out.append("  you rejected this: it shows as yours, not as spawned")
        elif row.get("review_changes"):
            out.append("  would move out of your list (shown as yours today)"
                       + ("; you accepted it" if verdict == "accepted" else ""))
        elif row.get("role") == "lead":
            out.append("  shown today as an agent-team lead; the spawn mark doesn't change that")
        else:
            out.append("  already hidden today for another reason (an agent run, a background"
                       " job or a /tmp directory)")
        for label, value in launches.explain(src.root, row["id"]):
            if label == "launched from" and hasattr(src.adapter, "session_title"):
                value = f"{src.adapter.session_title(src.root, value.split()[0])}  ({value})"
            first, *more = str(value).split("\n")
            out.append(f"  {label:<16} {first}")
            out += [f"  {'':<16} {m}" for m in more[:12]]
        out.append("\n  1 accept (it was spawned)   2 reject (I started it)   3 needs discussion   0 clear\n")
    opening = d.get("opening")
    same = bool(opening and d.get("prompt")
                and str(opening).strip() == str(d["prompt"]).strip())
    # Fixed order, always all three, so the pane has the same shape every time
    # and a missing piece is visible rather than silently absent.
    for label, body, limit in (("last prompt", d.get("prompt"), 700),
                               (f"last msg from {src.adapter.ASSISTANT}", d.get("reply"), 700),
                               ("started with", opening, 500)):
        out.append(f"▸ {label}\n")
        if label == "started with" and same:
            # A one-exchange session: printing it twice wastes the pane, and
            # "(no record)" would wrongly say it is missing.
            out.append("  (the same as the last prompt)")
        elif body:
            wrap_into(out, str(body), max(30, width - 4), limit)
        else:
            out.append("  (no record)")
        out.append("")
    return safe_text("\n".join(out))


def show_path(row, home):
    path = row["cwd"]
    if not path:
        return "-"
    if row["source"].host == this_host():
        path = path.replace(home, "~", 1)
    path += "?" if row["guessed"] else ""
    return "…" + path[-(DIRW - 1):] if len(path) > DIRW else path


def fmt(row, namew, home, now):
    shown = row["name"]
    prefix = f"[{row['review_mark']}] " if "review_mark" in row else ""
    room = max(namew - len(prefix), 2)             # the mark lives inside the NAME column
    if len(shown) > room:
        shown = shown[: room - 1] + "…"
    shown = prefix + shown
    mark = "·" if row["named"] else " "
    size = row.get("size_text") or size_str(row["bytes"])
    return (f'{when(row["mtime"], now):>20}  {row["source"].tag:<{SRCW}} {row["role"]:<4} '
            f'{mark}{shown:<{namew}}  {show_path(row, home):<{DIRW}}  {size:>9}')


def header(namew):
    return (f'{"LAST ACTIVE":>20}  {"SOURCE":<{SRCW}} {"ROLE":<4} {"":1}{"NAME":<{namew}}  '
            f'{"DIRECTORY":<{DIRW}}  {"SIZE":>9}')


def plain(rows, total, sources):
    home = str(Path.home())
    now = time.time()
    namew = min(max((len(r["name"]) for r in rows), default=4), 40)
    print(header(namew) + "  ID")
    for r in rows:
        print(safe_text(fmt(r, namew, home, now) + f'  {r["id"]}', line=True))
    print(f'\n{len(rows)} shown, {total} sessions in {len(sources)} stores'
          f'   · = named by you, ? = directory not recorded or inferred', file=sys.stderr)
    print("resume one:  hopback --print-cd <id or words>   (or run hopback for the picker)",
          file=sys.stderr)


def fuzzy(needle, haystack):
    """fzf-style match: every space-separated word must appear in order as a
    subsequence of the haystack, case-insensitively, in any word order."""
    hay = haystack.lower()
    for word in needle.lower().split():
        it = iter(hay)
        if not all(ch in it for ch in word):
            return False
    return True


def rank(rows, needle):
    """Rows matching `needle`, the best matches first.

    A row where every word appears as a real substring outranks one that only
    matches as a scattered subsequence (fzf's own priority), and newest-first
    order is kept within each group, because the sort is stable.
    """
    hits = [r for r in rows if fuzzy(needle, haystack(r))]
    words = needle.lower().split()
    return sorted(hits, key=lambda r: not all(w in haystack(r).lower() for w in words))


def haystack(row):
    src = row["source"]
    return f'{row["name"]} {row["cwd"]} {row["id"]} {src.tag} {src.label} {row["role"]}'


def keep(row, show_agents, show_scheduled, show_leftovers=False):
    role = row.get("role", "")
    if role in AGENT and not show_agents:
        return False
    if role in LEFTOVER and not show_leftovers:
        return False
    return not (role in SCHEDULED and not show_scheduled)


def hidden_path():
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "hopback" / "hidden.json"


def hide_key(row):
    """Session ids are only unique within a harness, so the key names both."""
    return f'{row["source"].adapter.NAME}:{row["id"]}'


# What went wrong reading saved state, for the picker (or the plain list) to show.
STATE_PROBLEMS = []


def _read_state(path, kind):
    """The JSON in `path` if it holds a `kind`, else None. Missing: None. A
    damaged file is moved aside (kept, never overwritten) and reported in
    STATE_PROBLEMS. Unreadable for any other reason: OSError propagates, so
    nothing saves over a file that couldn't be read."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)             # bytes: bad UTF-8 is a ValueError too
    except (ValueError, RecursionError):
        data = None
    if isinstance(data, kind):
        return data
    # Move it aside FIRST, then check that what moved is what was judged: a
    # second hopback may have replaced it with a good file in between.
    # Unique to the nanosecond: a second damaged copy never replaces the first.
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}.{time.time_ns() % 10**9:09d}"
    aside = path.with_name(f"{path.name}.damaged-{stamp}-{os.getpid()}")
    try:
        os.replace(path, aside)
    except FileNotFoundError:
        return None                        # another hopback moved it, and reports it
    if aside.read_bytes() != raw:          # a good save landed first: put it back
        try:
            os.link(aside, path)
        except FileExistsError:
            pass                           # and a newer one since: that one stands
        aside.unlink()
        return _read_state(path, kind)
    STATE_PROBLEMS.append(f"{path} was damaged; kept as {aside.name}, starting from empty")
    return None


def load_hidden():
    data = _read_state(hidden_path(), list) or []
    return {k for k in data if isinstance(k, str)}


REVIEW_MARKS = {"accepted": "✓", "rejected": "✗", "discuss": "?", None: " "}


def review_path():
    return hidden_path().with_name("review.json")


def load_review():
    """{session id: "accepted" | "rejected" | "discuss"}: Phil's verdicts on
    sessions the spawn detector marked. Entries that aren't one of those are
    skipped; a damaged file is handled by _read_state."""
    data = _read_state(review_path(), dict) or {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v in REVIEW_MARKS}


def save_review(marks):
    _write_state(review_path(), marks)


def apply_review(rows, marks):
    """A session Phil rejected as "not spawned" goes back to looking like his;
    any other verdict, or none, leaves the detector's `spawn` in place. Safe to
    call again after a mark changes."""
    for r in rows:
        if "spawn_team" not in r and r.get("role") == "spawn":
            r["spawn_team"] = r["team"]            # remember what the detector said
        if "spawn_team" in r:
            rejected = marks.get(r["id"]) == "rejected"
            r["role"], r["team"] = ("", "") if rejected else ("spawn", r["spawn_team"])
    return rows


def review_rows(sources):
    """(rows, problems): every session the spawn detector marked, from every
    Claude Code store, whatever its age, the ones that change what the list
    shows first; and what went wrong, for the Review view to show."""
    from . import launches
    out, problems = [], []
    for src in sources:
        if src.adapter.NAME != "claude":
            continue
        try:
            # Its problems come back through load_rows below, which runs the
            # same scan inside collect(); collecting them here too would
            # show each twice.
            flagged = launches.started_by(src.root, [])
        except Exception as exc:  # noqa: BLE001 - shown in the Review view
            problems.append(f"{src.tag}: spawn detection failed ({exc.__class__.__name__}: {exc})")
            continue
        rows, _, warns = load_rows([src], None, False, False, include_teams=True,
                                   include_scratch=True, include_empty=True, only=set(flagged))
        problems += warns
        for r in rows:
            r["review"] = True
            # Shown as yours today: its only reason to be hidden is this rule.
            r["review_changes"] = (r["role"] == "spawn" and not (
                r["cwd"] == "/tmp" or str(r["cwd"]).startswith("/tmp/")))
            out.append(r)
    out.sort(key=lambda r: (not r["review_changes"], -r["mtime"]))
    return out, problems


def _write_state(path, data):
    """Written to a temp file and renamed, so a crash mid-write can't empty
    it; a failed write leaves no temp file behind and raises."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def save_hidden(keys):
    _write_state(hidden_path(), sorted(keys))


# --------------------------------------------------------------------------
# Textual UI
# --------------------------------------------------------------------------

def _exit_with_parent(parent):
    """A worker whose picker was killed (SIGTERM, a closed terminal) would wait
    on its queue forever; poll for the parent and exit when it is gone."""
    import threading

    def watch():
        while os.getppid() == parent:
            time.sleep(1)
        os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def _pool_init(parent, stores):
    """Each worker watches its parent and, on a daemon thread, reads the cost
    rates of every store once (the scan is per process, and would otherwise
    cost the first preview that needs a rate a second or two). It is a thread
    so the worker takes jobs at once: a preview that needs no rate does not
    wait, and one that does waits on the scan's lock rather than scanning
    again. Returns the thread (the pool ignores it)."""
    import threading
    _exit_with_parent(parent)

    def warm():
        for store in stores:
            claude.warm_rates(Path(store))
    thread = threading.Thread(target=warm, name="hopback-warm-rates", daemon=True)
    thread.start()
    return thread


def preview_pool(stores=()):
    """Worker processes for the preview pane, started before Textual takes over
    stdio (spawning afterwards fails on its replaced file descriptors). spawn,
    not fork: forking a process that runs threads can deadlock the child.
    `stores`: the Claude project directories whose cost rates the workers read
    as they start."""
    import concurrent.futures
    import multiprocessing
    try:
        pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=2, mp_context=multiprocessing.get_context("spawn"),
            initializer=_pool_init, initargs=(os.getpid(), tuple(str(s) for s in stores)))
        for _ in range(2):
            pool.submit(int)   # start both workers now, while startup is under way
    except (OSError, ImportError, NotImplementedError, ValueError):
        return None            # previews then run in a thread instead
    return pool


def build_app(rows, query, yolo, total, here_only, sources, harness=None, host=None,
              show_agents=False, show_scheduled=False, show_leftovers=False, warnings=(),
              start_hidden=False, start_review=False):
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from rich.segment import Segment
    from rich.style import Style
    from textual.geometry import Size
    from textual.message import Message
    from textual.scroll_view import ScrollView
    from textual.strip import Strip
    from textual.widgets import Input, Static, Tab, Tabs

    harnesses = [a for a in BY_NAME if any(s.adapter.NAME == a for s in sources)]
    hosts = sorted({s.host for s in sources}, key=lambda h: (h != this_host(), h))

    # Row colours: the old ListItem CSS (base, "alt" stripe, hover, highlight).
    ROW_STYLE = Style(bgcolor="#16161e", color="#a9b1d6")
    ROW_ALT = Style(bgcolor="#262b3d", color="#a9b1d6")
    ROW_HOVER = Style(bgcolor="#343b58", color="#c0caf5")
    ROW_HIGHLIGHT = Style(bgcolor="#3b4261", color="#ffc896", bold=True)

    class SessionList(ScrollView):
        """The session rows, painted line by line.

        One widget per row (ListView) re-styles and re-lays-out every row on
        each cursor move, tens of milliseconds even for a hundred rows.
        Painting only the visible lines keeps a move to a couple of
        milliseconds at any length. Never focused: typing always goes to the
        search box, and the app's bindings move the cursor.

        The wheel changes the SELECTION rather than the viewport: scrolling
        under a stationary cursor means the highlighted row silently becomes
        one you are not pointing at.
        """

        can_focus = False

        class Highlighted(Message):
            pass

        class Selected(Message):
            pass

        def __init__(self, **kw):
            super().__init__(**kw)
            self.lines = []
            self._index = None
            self.hover = None

        @property
        def index(self):
            return self._index

        @index.setter
        def index(self, value):
            if not self.lines:
                value = None
            elif value is not None:
                value = max(0, min(value, len(self.lines) - 1))
            if value == self._index:
                return
            self._index = value
            if value is not None:
                top, h = int(self.scroll_offset.y), max(1, self.scrollable_content_region.height)
                if value < top:
                    self.scroll_to(y=value, animate=False, immediate=True)
                    self.hover = None      # the pointer is over another row now
                elif value >= top + h:
                    self.scroll_to(y=value - h + 1, animate=False, immediate=True)
                    self.hover = None      # (the next mouse move sets it again)
            self.refresh()
            self.post_message(self.Highlighted())

        def set_lines(self, lines, index, keep_scroll=False):
            """Replace every row; the cursor lands on `index` (clamped). With
            `keep_scroll` the viewport stays where it was (as far as the new
            length allows) instead of going back to the top."""
            top = int(self.scroll_offset.y)
            self.lines = [safe_text(t, line=True) for t in lines]
            self.hover = None
            self._index = None
            self.virtual_size = Size(0, len(lines))
            self.scroll_to(y=top if keep_scroll else 0, animate=False, immediate=True)
            self.index = index if lines else None
            self.refresh()

        def set_line(self, i, text):
            """Change one row's text in place; cursor and scroll stay."""
            self.lines[i] = safe_text(text, line=True)
            self.refresh()

        def render_line(self, y):
            width = self.scrollable_content_region.width
            idx = y + int(self.scroll_offset.y)
            if idx >= len(self.lines):
                return Strip.blank(width, ROW_STYLE)
            if idx == self._index:
                style = ROW_HIGHLIGHT
            elif idx == self.hover:
                style = ROW_HOVER
            else:
                style = ROW_ALT if idx % 2 else ROW_STYLE
            return Strip([Segment(" " + self.lines[idx], style)]).crop_extend(0, width, style)

        def move(self, step):
            self.index = 0 if self._index is None else self._index + step

        def _row_at(self, event):
            idx = event.y + int(self.scroll_offset.y)
            return idx if 0 <= idx < len(self.lines) else None

        def on_mouse_scroll_down(self, event):
            event.prevent_default()
            event.stop()
            self.move(1)

        def on_mouse_scroll_up(self, event):
            event.prevent_default()
            event.stop()
            self.move(-1)

        def on_mouse_move(self, event):
            idx = self._row_at(event)
            if idx != self.hover:
                self.hover = idx
                self.refresh()

        def on_leave(self, event):
            if self.hover is not None:
                self.hover = None
                self.refresh()

        def on_click(self, event):
            idx = self._row_at(event)
            if idx is not None:
                self.index = idx
                self.post_message(self.Selected())

    class SearchBox(Input):
        """In the review view, with the search box empty, the digits 0-3 mark
        the selected session instead of being typed. Once you start a search
        they type as usual (the clickable controls still mark)."""

        async def _on_key(self, event):
            if (self.app.view_review and not self.value
                    and event.character in ("0", "1", "2", "3")):
                event.stop()
                event.prevent_default()
                self.app.action_mark({"1": "accepted", "2": "rejected", "3": "discuss"}
                                     .get(event.character))
                return
            await super()._on_key(event)

    class Clickable(Static):
        """A one-line control that runs an app action when clicked."""

        def __init__(self, action, **kw):
            super().__init__("", markup=False, **kw)
            self.on_click_action = action

        async def on_click(self, event):
            event.stop()
            await self.app.run_action(self.on_click_action)

    class SessionPicker(App):
        CSS = """
        Screen { background: #16161e; }
        #frame {
            border: round #3a3a4a;
            border-title-color: #7aa2f7;
            padding: 0 1;
            height: 100%;
        }
        #search { border: none; background: #1a1b26; color: #c0caf5; height: 1; }
        #search:focus { border: none; }
        #tabrow { height: 2; }
        #tabs { height: 2; width: 1fr; background: #16161e; }
        #hiddentab { width: auto; height: 1; color: #565f89; padding: 0 2; }
        #hiddentab:hover { color: #c0caf5; }
        #hiddentab.active { color: #ffc896; text-style: bold underline; }
        #reviewtab { width: auto; height: 1; color: #bb9af7; padding: 0 2; }
        #reviewtab:hover { color: #c0caf5; }
        #reviewtab.active { color: #ffc896; text-style: bold underline; }
        #tabs Tab { color: #565f89; padding: 0 2; }
        #tabs Tab.-active { color: #ffc896; text-style: bold; }
        #head { color: #7dcfff; height: auto; }
        #warn { color: #f7768e; height: auto; }
        .control { color: #e0af68; height: 1; width: auto; margin-right: 4; }
        .control:hover { background: #2f3549; color: #ffc896; }
        .controls { height: 1; padding-left: 2; }
        #keys { height: auto; }
        #labels { color: #7dcfff; height: 1; }
        #list { background: #16161e; height: 1fr; overflow-x: hidden; scrollbar-size-vertical: 1; }
        #preview {
            height: 45%; border-top: solid #3a3a4a; background: #16161e;
            padding: 0 1; scrollbar-size-vertical: 1;
        }
        #preview_body { color: #a9b1d6; height: auto; }
        .hidden { display: none; }
        """
        BINDINGS = [
            Binding("escape", "quit_none", "quit", show=False),
            Binding("ctrl+c", "quit_none", "quit", show=False),
            Binding("enter", "resume", "resume", show=False),
            # priority=True: the search Input has focus and would otherwise
            # swallow these before the app ever sees them.
            Binding("ctrl+y", "resume_yolo", "skip permissions", show=False, priority=True),
            Binding("ctrl+t", "toggle_agents", "agents", show=False, priority=True),
            Binding("ctrl+r", "toggle_scheduled", "scheduled", show=False, priority=True),
            Binding("ctrl+b", "toggle_leftovers", "background-job leftovers", show=False, priority=True),
            Binding("ctrl+x", "toggle_hide", "hide or unhide this session", show=False, priority=True),
            Binding("ctrl+g", "show_hidden", "hidden sessions", show=False, priority=True),
            Binding("f12", "toggle_review", "review spawn detection", show=False, priority=True),
            Binding("ctrl+o", "cycle_host", "hosts", show=False, priority=True),
            Binding("tab", "cycle_tab(1)", "next tab", show=False, priority=True),
            Binding("shift+tab", "cycle_tab(-1)", "previous tab", show=False, priority=True),
            Binding("ctrl+underscore", "toggle_preview", "preview", show=False, priority=True),
            Binding("ctrl+slash", "toggle_preview", "preview", show=False, priority=True),
            Binding("ctrl+j", "cursor_down", "down", show=False, priority=True),
            Binding("ctrl+k", "cursor_up", "up", show=False, priority=True),
            Binding("ctrl+u", "clear_query", "clear", show=False, priority=True),
            Binding("down", "cursor_down", "down", show=False),
            Binding("up", "cursor_up", "up", show=False),
            Binding("pagedown", "page_down", "page down", show=False),
            Binding("pageup", "page_up", "page up", show=False),
        ]

        def __init__(self):
            super().__init__()
            self.all_rows = rows
            self.search_text = query or ""
            self.yolo = yolo
            self.show_agents = show_agents
            self.show_scheduled = show_scheduled
            self.show_leftovers = show_leftovers
            self.harness = harness   # None = every harness
            self.host = host         # None = every host
            self.result = None
            self._visible = []
            self.toggle_text = ""
            self.sched_text = ""
            self.left_text = ""
            self.host_text = ""
            self.head_text = ""
            self._previews = {}
            self._preview_key = None
            self._preview_timer = None
            self._pool = preview_pool(sorted({str(s.root / "projects") for s in sources
                                              if s.adapter is claude}))
            self.hidden = load_hidden()
            self.view_hidden = start_hidden
            self.review = load_review()
            self._unsaved = {}             # marks a failed save hasn't written yet
            self._namew = 4
            self._refresh_lock = asyncio.Lock()
            self._refresh_ticket = 0       # the newest rebuild requested
            self._shown_ticket = 0         # the one the rows on screen came from
            apply_review(rows, self.review)
            self.review_rows = None        # loaded the first time the view opens
            self.review_on = start_review  # the Review tab is shown
            self.view_review = False
            self._review_loading = False

        def compose(self) -> ComposeResult:
            with Vertical(id="frame") as frame:
                frame.border_title = "  hopback  "
                yield SearchBox(placeholder="search", id="search", value=self.search_text)
                with Horizontal(id="tabrow"):
                    yield Tabs(Tab("All", id="t-all"),
                               *(Tab(BY_NAME[h].LABEL, id=f"t-{h}") for h in harnesses),
                               id="tabs",
                               active="" if self.view_hidden or start_review
                               else f"t-{self.harness or 'all'}")
                    # Outside the Tabs widget on purpose: TAB cycles harnesses
                    # only, and this view is reached by click or CTRL-G.
                    yield Clickable("show_hidden", id="hiddentab")
                    yield Clickable("show_review", id="reviewtab",
                                    classes="" if start_review else "hidden")
                yield Static("", id="head", markup=False)
                yield Static(safe_text("\n".join(warnings)), id="warn", markup=False,
                             classes="" if warnings else "hidden")
                with Horizontal(id="controls", classes="controls"):
                    yield Clickable("cycle_host", id="hosts", classes="control")
                    yield Clickable("toggle_hide", id="hide", classes="control")
                # One row per kind of thing kept out of the list, so every
                # toggle is visible, and clickable, even when it hides nothing.
                with Horizontal(id="reviewbar", classes="controls hidden"):
                    yield Clickable("mark('accepted')", id="m-acc", classes="control")
                    yield Clickable("mark('rejected')", id="m-rej", classes="control")
                    yield Clickable("mark('discuss')", id="m-dis", classes="control")
                    yield Clickable("mark(None)", id="m-clr", classes="control")
                with Horizontal(id="toggles", classes="controls"):
                    yield Clickable("toggle_agents", id="toggle", classes="control")
                    yield Clickable("toggle_scheduled", id="sched", classes="control")
                    yield Clickable("toggle_leftovers", id="left", classes="control")
                yield Static("", id="keys")
                yield Static("", id="labels", markup=False)
                yield SessionList(id="list")
                with VerticalScroll(id="preview"):
                    yield Static("", id="preview_body", markup=False)

        async def on_mount(self):
            claude.STOP.clear()
            await self.refresh_rows()
            self.query_one("#search", Input).focus()
            self.report_state()
            if self.review_on:
                self.action_show_review()

        def on_unmount(self):
            claude.STOP.set()      # a rate scan in the thread fallback ends at its next file
            # Kill, not just shut down: ENTER execs the agent straight after
            # this, and a worker mid-preview would outlive hopback inside it.
            if self._pool:
                for proc in list(getattr(self._pool, "_processes", {}).values()):
                    proc.kill()
                self._pool.shutdown(wait=False, cancel_futures=True)

        # -- data ------------------------------------------------------------
        def in_scope(self, r, ignore_harness=False):
            """Host, harness and toggles, but not the search text. The hidden
            view shows every session you hid, from any harness, and nothing else."""
            src = r["source"]
            if self.host and src.host != self.host:
                return False
            if (hide_key(r) in self.hidden) != self.view_hidden:
                return False
            if self.view_hidden:
                return True
            if not ignore_harness and self.harness and src.adapter.NAME != self.harness:
                return False
            return keep(r, self.show_agents, self.show_scheduled, self.show_leftovers)

        def visible_rows(self):
            if self.view_review:
                rs = [dict(r, review_mark=REVIEW_MARKS[self.review.get(r["id"])],
                           review_verdict=self.review.get(r["id"]))
                      for r in self.review_rows or []]
                return rank(rs, self.search_text) if self.search_text else rs
            rs = [r for r in self.all_rows if self.in_scope(r)]
            if self.search_text:
                rs = rank(rs, self.search_text)
            return rs

        def refresh_rows(self, keep_index=True):
            """A coroutine that rebuilds the list from the current state. The
            ticket is taken NOW, at request time, so _current() knows the
            rows on screen are stale before the rebuild has even started."""
            self._refresh_ticket += 1
            return self._rebuild(self._refresh_ticket, keep_index)

        async def _rebuild(self, ticket, keep_index):
            """Rebuilds queue on a lock, and one a newer request has overtaken
            returns at once: each reads the current state, so only the newest
            needs to run. (With the old ListView this was needed because
            cancelling clear()/extend() part way froze the picker; set_lines
            is synchronous now, so the lock is kept as a guard, not a need.)"""
            async with self._refresh_lock:
                if ticket != self._refresh_ticket:
                    return
                lv = self.query_one("#list", SessionList)
                self._visible = self.visible_rows()
                if keep_index:
                    prev = lv.index
                elif self.view_review:             # start where the work is: the first unmarked
                    prev = next((i for i, r in enumerate(self._visible) if not r["review_verdict"]), 0)
                else:
                    prev = 0
                home = str(Path.home())
                now = time.time()
                namew = min(max((len(r["name"]) for r in self._visible), default=4), 34)
                if self.view_review:
                    namew += 4                     # room for the "[x] " mark fmt() puts in the column
                self._namew = namew
                lv.set_lines([fmt(r, namew, home, now) for r in self._visible], prev or 0,
                             keep_scroll=keep_index)
                self._shown_ticket = ticket
                self.update_header(namew)
                self.update_preview()

        def update_header(self, namew):
            shown = len(self._visible)
            def scoped(hidden_view):
                was, self.view_hidden = self.view_hidden, hidden_view
                try:
                    return [r for r in self.all_rows if self.in_scope(r, ignore_harness=True)]
                finally:
                    self.view_hidden = was
            shown_rows, hid_rows = scoped(False), scoped(True)

            def label(name, h):
                n = sum(1 for r in shown_rows if not h or r["source"].adapter.NAME == h)
                k = sum(1 for r in hid_rows if not h or r["source"].adapter.NAME == h)
                return f"{name} {n:,}" + (f" (+{k:,} hidden)" if k else "")
            tabs = self.query_one("#tabs", Tabs)
            tabs.query_one("#t-all", Tab).label = label("All", None)
            for h in harnesses:
                tabs.query_one(f"#t-{h}", Tab).label = label(BY_NAME[h].LABEL, h)
            ht = self.query_one("#hiddentab", Static)
            ht.update(f"Hidden {len(hid_rows):,}")
            ht.set_class(self.view_hidden, "active")
            rt = self.query_one("#reviewtab", Static)
            rt.set_class(not self.review_on, "hidden")
            rt.set_class(self.view_review, "active")
            if self.review_rows is not None:
                done = sum(1 for r in self.review_rows if r["id"] in self.review)
                rt.update(f"Review {done:,}/{len(self.review_rows):,}")
            else:
                rt.update("Review")
            self.query_one("#reviewbar").set_class(not self.view_review, "hidden")
            if self.view_review and self.review_rows is not None:
                rs = self.review_rows
                count = {v: sum(1 for r in rs if self.review.get(r["id"]) == v)
                         for v in ("accepted", "rejected", "discuss")}
                changes = sum(1 for r in rs if r["review_changes"])
                self.query_one("#m-acc", Static).update(f"[ 1 ] accept, it was spawned ({count['accepted']})")
                self.query_one("#m-rej", Static).update(f"[ 2 ] reject, I started it ({count['rejected']})")
                self.query_one("#m-dis", Static).update(f"[ 3 ] needs discussion ({count['discuss']})")
                self.query_one("#m-clr", Static).update(
                    f"[ 0 ] clear    {len(rs) - sum(count.values())} open · "
                    f"first {changes} change your list")
            scope = ("this directory only" if here_only
                     else f"all directories, {total:,} on disk")
            self.query_one("#head", Static).update(
                f"  {shown:,} sessions · {scope} · newest first\n"
                f"  ENTER resume    CTRL-Y resume skipping permissions    TAB harness    "
                f"CTRL-G hidden    CTRL-/ preview    ESC quit")
            self.head_text = f"{shown} sessions"

            per_host = {h: sum(1 for r in self.all_rows if r["source"].host == h
                               and hide_key(r) not in self.hidden
                               and keep(r, self.show_agents, self.show_scheduled, self.show_leftovers)
                               and (not self.harness or r["source"].adapter.NAME == self.harness))
                        for h in hosts}
            counts = " · ".join(f"{HOSTS.get(h, h)} {per_host[h]:,}" for h in hosts)
            self.host_text = (f"[ CTRL-O ] hosts: {HOSTS[self.host] if self.host else 'all'}  ({counts})")
            self.query_one("#hosts", Static).update(self.host_text)

            def hidden(roles):
                return sum(1 for r in self.all_rows if r.get("role") in roles
                           and hide_key(r) not in self.hidden
                           and (not self.host or r["source"].host == self.host)
                           and (not self.harness or r["source"].adapter.NAME == self.harness))
            n_agents, n_sched = hidden(AGENT), hidden(SCHEDULED)
            self.toggle_text = (f"[ CTRL-T ] agent sessions {'shown' if self.show_agents else 'hidden'}"
                                f" ({n_agents:,})")
            self.sched_text = (f"[ CTRL-R ] scheduled runs {'shown' if self.show_scheduled else 'hidden'}"
                               f" ({n_sched:,})")
            self.query_one("#toggle", Static).update(self.toggle_text)
            sched = self.query_one("#sched", Static)
            sched.update(self.sched_text)
            n_left = hidden(LEFTOVER)
            self.left_text = (f"[ CTRL-B ] job leftovers "
                              f"{'shown' if self.show_leftovers else 'hidden'} ({n_left:,})")
            self.query_one("#left", Static).update(self.left_text)
            self.query_one("#hide", Static).update(
                "[ CTRL-X ] unhide this session" if self.view_hidden
                else "[ CTRL-X ] hide this session")
            # The toggles mean nothing in the hidden and review views.
            self.query_one("#toggles").set_class(self.view_hidden or self.view_review, "hidden")
            self.query_one("#hide").set_class(self.view_review, "hidden")

            def chip(tok, desc):
                return f"[b #1a1b26 on #7dcfff] {tok} [/][#565f89] {desc}[/]"
            present = {r.get("role") for r in self.all_rows}
            chips = [chip(k, v) for k, v in ROLES.items() if k in present]
            chips += [chip("·", "named by you"), chip("?", "directory not recorded / inferred")]
            per_line = 5
            lines = ["  " + "   ".join(chips[i:i + per_line])
                     for i in range(0, len(chips), per_line)]
            self.query_one("#keys", Static).update("\n".join(lines))
            self.query_one("#labels", Static).update("  " + header(namew))

        def update_preview(self):
            """Show the highlighted row's preview without ever blocking the UI.

            Reading a large transcript (cost alone reads the whole file, about
            a second at 190 MB) runs in a separate process: in a thread, its
            JSON parsing holds the GIL and the UI still stutters. Results are
            cached per session, so moving back to a row is instant; a cached
            result older than 10 s is shown and then refreshed. A result for a
            row the cursor has already left is dropped.
            """
            pane = self.query_one("#preview_body", Static)
            lv = self.query_one("#list", SessionList)
            if self._preview_timer:
                self._preview_timer.stop()
            if not self._visible or lv.index is None:
                self._preview_key = None
                pane.update("  scanning for spawned sessions… (a few seconds the first time)"
                            if self.view_review and self.review_rows is None else "")
                return
            row = self._visible[min(lv.index, len(self._visible) - 1)]
            width = max(40, self.size.width - 6)
            key = (row["source"].tag, row["id"], width, bool(row.get("review")),
                   row.get("review_verdict"), row.get("role"))
            self._preview_key = key
            cached = self._previews.get(key)
            if cached:
                show(pane, cached[1])
                # A live session keeps growing: show what we have at once, and
                # re-read it in the background if that is more than 10 s old.
                if time.time() - cached[0] < 10:
                    return
            else:
                show(pane, f"{row['name']}\n\n  loading…")
            # Debounce: holding an arrow key fires one read for the row it
            # stops on, not one per row passed. exclusive: moving on cancels a
            # read that hasn't started, so the row you stop on isn't queued
            # behind every row you passed.
            self._preview_timer = self.set_timer(
                0.06, lambda: self.run_worker(self.load_preview(row, width, key),
                                              group="preview", exclusive=True))

        async def load_preview(self, row, width, key):
            if key != self._preview_key:
                return
            from concurrent.futures.process import BrokenProcessPool
            loop = asyncio.get_running_loop()
            try:
                text = await loop.run_in_executor(self._pool, preview, row, width)
            except BrokenProcessPool:
                # A worker died (out of memory on a huge file, say). A new pool
                # can't be spawned under Textual, so carry on in a thread.
                self._pool = None
                try:
                    text = await loop.run_in_executor(None, preview, row, width)
                except Exception as exc:  # noqa: BLE001
                    if key == self._preview_key:
                        show(self.query_one("#preview_body", Static),
                             f"could not read this session: {exc.__class__.__name__}: {exc}")
                    return      # not cached: the next visit tries again
            except Exception as exc:  # noqa: BLE001
                text = safe_text(f"could not read this session: {exc.__class__.__name__}: {exc}")
                if key == self._preview_key:
                    show(self.query_one("#preview_body", Static), text)
                return          # not cached: the next visit tries again
            self.show_preview(key, text)

        def show_preview(self, key, text):
            if len(self._previews) > 200:
                self._previews.clear()
            self._previews[key] = (time.time(), text)
            if key == self._preview_key:
                show(self.query_one("#preview_body", Static), text)

        # -- events ----------------------------------------------------------
        def on_input_submitted(self, event):
            # The search box has focus so it receives Enter first; without this
            # the key never reaches the list and nothing happens.
            self.action_resume()

        async def on_input_changed(self, event):
            self.search_text = event.value
            await self.refresh_rows(keep_index=False)

        def on_tabs_tab_activated(self, event):
            want = None if event.tab.id == "t-all" else event.tab.id[2:]
            # Clicking a tab moves focus to the tab bar; typing should keep
            # going to the search box.
            self.query_one("#search", Input).focus()
            if want != self.harness or self.view_hidden or self.view_review:
                self.harness = want
                self.view_hidden = self.view_review = False
                self.run_worker(self.refresh_rows(keep_index=False))

        def on_session_list_highlighted(self, event):
            self.update_preview()

        def on_session_list_selected(self, event):
            self.action_resume()

        # -- actions ---------------------------------------------------------
        def action_cursor_down(self):
            self.query_one("#list", SessionList).move(1)

        def action_cursor_up(self):
            self.query_one("#list", SessionList).move(-1)

        def action_page_down(self):
            self.query_one("#list", SessionList).move(10)

        def action_page_up(self):
            self.query_one("#list", SessionList).move(-10)

        def action_clear_query(self):
            self.query_one("#search", Input).value = ""

        def action_cycle_tab(self, step):
            order = [None] + harnesses
            # From the hidden view, TAB returns to the harness you came from.
            nxt = (self.harness if self.view_hidden or self.view_review
                   else order[(order.index(self.harness) + step) % len(order)])
            # Setting the active tab fires TabActivated, which refreshes.
            self.query_one("#tabs", Tabs).active = f"t-{nxt or 'all'}"

        def action_cycle_host(self):
            if self.view_review:
                return
            order = [None] + hosts
            self.host = order[(order.index(self.host) + 1) % len(order)]
            self.run_worker(self.refresh_rows(keep_index=False))

        def action_toggle_agents(self):
            if self.view_hidden or self.view_review:    # those views ignore filters
                return
            self.show_agents = not self.show_agents
            self.run_worker(self.refresh_rows(keep_index=False))

        def action_toggle_scheduled(self):
            if self.view_hidden or self.view_review:    # those views ignore filters
                return
            self.show_scheduled = not self.show_scheduled
            self.run_worker(self.refresh_rows(keep_index=False))

        def action_toggle_review(self):
            """F12: show the Review tab and open it; again: hide it."""
            if self.view_review:
                self.review_on = False
                self.action_cycle_tab(0)
                return
            self.review_on = True
            self.action_show_review()

        def action_show_review(self):
            if self.view_review:               # a click on the open tab closes it
                self.action_toggle_review()
                return
            self.view_review, self.view_hidden = True, False
            self.query_one("#tabs", Tabs).active = ""
            if self.review_rows is None:
                if not self._review_loading:
                    self._review_loading = True
                    self.run_worker(self.load_review_rows, thread=True, group="review")
                # Empty the list meanwhile, so nothing stale is on show.
                self.run_worker(self.refresh_rows(keep_index=False))
            else:
                self.run_worker(self.refresh_rows(keep_index=False))

        def load_review_rows(self):
            rows, problems = review_rows(sources)
            self.call_from_thread(self.review_loaded, rows, problems)

        def review_loaded(self, rows, problems=()):
            self._review_loading = False
            self.review_rows = rows
            for p in problems:
                self.notify(safe_text(p), severity="error", timeout=30)
            if self.view_review:
                self.run_worker(self.refresh_rows(keep_index=False))

        def action_mark(self, verdict):
            """Record Phil's verdict on the selected session, then move on.

            Synchronous: the row, the counts and the cursor change before the
            next key is read, so marking quickly never drops or misplaces one."""
            lv = self.query_one("#list", SessionList)
            if not self.view_review:
                return
            # Not while the scan is loading: the list on screen is not yet
            # the review list, so the mark would land on the wrong session.
            if self.review_rows is None:
                self.notify("still scanning: nothing marked yet")
                return
            cur = self._current()
            if cur is None:
                return
            i, row = cur
            # The file may have marks from another hopback: re-read it, then
            # apply this mark and any an earlier failed save still holds.
            self._unsaved[row["id"]] = verdict or None
            failed, seen = None, len(STATE_PROBLEMS)
            try:
                marks = load_review()
            except OSError as exc:             # unreadable: never save over it
                marks, failed = dict(self.review), exc
            if len(STATE_PROBLEMS) > seen:     # found damaged and set aside: keep what we hold
                marks = {**self.review, **marks}
            for sid, v in self._unsaved.items():
                if v:
                    marks[sid] = v
                else:
                    marks.pop(sid, None)
            self.review = marks
            if failed is None:
                try:
                    save_review(marks)
                    self._unsaved.clear()
                except OSError as exc:
                    failed = exc
            if failed is not None:
                self.notify(f"couldn't save the verdict ({failed}); kept here, saved with the next mark",
                            severity="warning")
            self.report_state()
            apply_review(self.all_rows, self.review)   # shows at once in the main list
            row["review_verdict"] = self.review.get(row["id"])
            row["review_mark"] = REVIEW_MARKS[row["review_verdict"]]
            lv.set_line(i, fmt(row, self._namew, str(Path.home()), time.time()))
            self.update_header(self._namew)
            if i + 1 < len(self._visible):
                lv.index = i + 1                       # the highlight event refreshes the preview
            else:
                self.update_preview()                  # same row, new verdict

        def on_key(self, event):
            """The mark keys work wherever focus is: should something other
            than the search box hold it, digits bubble up to here. (The search
            box handles them itself, typing them once a search has begun.)"""
            if (self.view_review and event.character in ("0", "1", "2", "3")
                    and not isinstance(self.focused, Input)):
                event.stop()
                self.action_mark({"1": "accepted", "2": "rejected", "3": "discuss"}
                                 .get(event.character))

        def report_state(self):
            """Show (once) what went wrong reading saved state."""
            while STATE_PROBLEMS:
                self.notify(safe_text(STATE_PROBLEMS.pop(0)), severity="warning", timeout=30)

        def action_show_hidden(self):
            if self.view_hidden:
                return
            self.view_hidden, self.view_review = True, False
            # No harness tab is active while the hidden view is up; activating
            # one (click or TAB) leaves it.
            self.query_one("#tabs", Tabs).active = ""
            self.run_worker(self.refresh_rows(keep_index=False))

        def action_toggle_hide(self):
            if self.view_review:
                self.notify("leave the Review view to hide sessions")
                return
            cur = self._current()
            if cur is None:
                return
            key = hide_key(cur[1])
            hide = key not in self.hidden
            # Re-read first, and apply only this change: another open picker
            # may have changed the list since this one started.
            seen = len(STATE_PROBLEMS)
            try:
                fresh = load_hidden()
            except OSError as exc:             # unreadable: never save over it
                self.notify(f"couldn't read the hidden list ({exc}); nothing changed", severity="error")
                return
            # Found damaged and set aside: keep what this picker holds.
            self.hidden = fresh | self.hidden if len(STATE_PROBLEMS) > seen else fresh
            self.report_state()
            if hide:
                self.hidden.add(key)
            else:
                self.hidden.discard(key)
            try:
                save_hidden(self.hidden)
            except OSError as exc:
                self.notify(f"couldn't save the hidden list ({exc}); it lasts until you quit",
                            severity="warning")
            self.run_worker(self.refresh_rows())

        def action_toggle_leftovers(self):
            if self.view_hidden or self.view_review:    # those views ignore filters
                return
            self.show_leftovers = not self.show_leftovers
            self.run_worker(self.refresh_rows(keep_index=False))

        def action_toggle_preview(self):
            self.query_one("#preview").toggle_class("hidden")

        def _current(self):
            """(index, row) under the cursor, or None. None from the moment a
            rebuild is requested until it is on screen: the cursor and the
            rows then belong to different lists, so acting would hit the
            wrong session."""
            lv = self.query_one("#list", SessionList)
            if self._shown_ticket != self._refresh_ticket:
                self.notify("the list is updating, press again")
                return None
            if not self._visible or lv.index is None:
                return None
            i = min(lv.index, len(self._visible) - 1)
            return i, self._visible[i]

        def action_resume(self, yolo=False):
            cur = self._current()
            if cur is None:
                return
            self.result = (cur[1], self.yolo or yolo)
            self.exit()

        def action_resume_yolo(self):
            self.action_resume(yolo=True)

        def action_quit_none(self):
            self.result = None
            self.exit()

    return SessionPicker()


def pick(rows, query, yolo, total, here_only, sources, **kw):
    """Run the picker. Returns (row, yolo) or (None, yolo)."""
    try:
        app = build_app(rows, query, yolo, total, here_only, sources, **kw)
    except ImportError as exc:
        print(f"textual is not available: {exc}", file=sys.stderr)
        return None, yolo
    app.run()
    if app.result:
        return app.result
    return None, yolo


def doctor(sources):
    """For "I ran it and nothing happened": every condition that decides
    between picker, list and silent exit, and every store that was found."""
    try:
        import textual
        tv = textual.__version__
    except ImportError:
        tv = "NOT INSTALLED"
    print(f"hopback     {os.path.abspath(__file__)}")
    print(f"python      {sys.version.split()[0]}  ({sys.executable})")
    print(f"textual     {tv}")
    print(f"this host   {HOSTS.get(this_host(), this_host())}")
    print(f"stdin tty   {sys.stdin.isatty()}")
    print(f"stdout tty  {sys.stdout.isatty()}")
    print(f"TERM        {os.environ.get('TERM', '(unset)')}")
    print("\nstores found:")
    if not sources:
        print("  (none)")
    for src in sources:
        try:
            n = f"{src.adapter.count(src.root):,} sessions"
        except Exception as exc:  # noqa: BLE001
            n = f"UNREADABLE: {exc}"
        print(f"  {src.tag:<{SRCW}} {str(src.root):<48} {n}")
    mode = "picker" if sys.stdin.isatty() and sys.stdout.isatty() else "list"
    print(f"\nwould use   {mode}")
    if mode == "list":
        print("  (not a terminal on both ends: are you running this through a"
              " pipe, or from inside another program?)")


def main():
    ap = argparse.ArgumentParser(
        prog="hopback",
        description="Browse and resume AI coding-agent sessions from any directory, "
                    "harness and OS.",
        epilog=f"harnesses: {', '.join(BY_NAME)}. hosts: {', '.join(HOSTS)}.",
    )
    ap.add_argument("words", nargs="*", metavar="[harness] [filter]",
                    help="optional harness name, then words to pre-filter on")
    ap.add_argument("--host", choices=sorted(HOSTS), help="only sessions from this host")
    ap.add_argument("-n", type=int, default=300, metavar="N",
                    help="how many sessions to load per store (default 300; sessions you "
                         "rejected in Review come on top)")
    ap.add_argument("-a", "--all", action="store_true", help="load every session")
    ap.add_argument("-d", "--here", action="store_true", help="only the current directory")
    ap.add_argument("-e", "--empty", action="store_true",
                    help="include sessions that contain no messages at all")
    ap.add_argument("-s", "--scratch", action="store_true",
                    help="include sessions rooted in /tmp (hidden by default)")
    ap.add_argument("-t", "--teams", "--agents", dest="teams", action="store_true",
                    help="include agent sessions: teammates, subagents, SDK/exec runs")
    ap.add_argument("-r", "--scheduled", action="store_true",
                    help="include scheduled (cron) runs")
    ap.add_argument("-b", "--background", action="store_true",
                    help="include background-job leftovers: older copies of sessions that "
                         "moved into a background job, and runs in a job's scratch directory")
    ap.add_argument("--review", action="store_true",
                    help="open the picker on the Review view (F12): check each session "
                         "the spawn detector marked and accept or reject it")
    ap.add_argument("--hidden", action="store_true",
                    help="only the sessions you hid with CTRL-X (opens the picker's Hidden view)")
    ap.add_argument("--archived", action="store_true", help="include archived sessions")
    ap.add_argument("-l", "--list", action="store_true", help="print a list, no picker")
    ap.add_argument("-y", "--yolo", "--dangerous", dest="yolo", action="store_true",
                    help="resume skipping permission prompts (each harness's own flag)")
    ap.add_argument("--print-cd", action="store_true",
                    help="print the command for the pick instead of running it")
    ap.add_argument("--id", metavar="PREFIX", help="resolve an id prefix and exit")
    ap.add_argument("--deep", action="store_true",
                    help="read whole files; slower, finds titles set only at the start")
    ap.add_argument("--doctor", action="store_true",
                    help="list every store found and why the picker did or did not appear")
    ap.add_argument("--preview", metavar="ID", help=argparse.SUPPRESS)
    ap.add_argument("--preview-width", type=int, default=80, help=argparse.SUPPRESS)
    args = ap.parse_args()

    # "hopback codex stremio": a leading harness name selects that harness.
    words = list(args.words)
    harness = words.pop(0) if words and words[0] in BY_NAME else None
    text = " ".join(words)
    # The invoked name decides the default too: `claude-sessions` keeps meaning
    # Claude Code only, as it always did.
    if harness is None and Path(sys.argv[0]).name == "claude-sessions":
        harness = "claude"

    sources = discover()
    if args.doctor:
        doctor(sources)
        return
    if not sources:
        sys.exit("no session stores found (looked for ~/.claude, ~/.codex, ~/.hermes)")
    scoped = [s for s in sources if (not harness or s.adapter.NAME == harness)
              and (not args.host or s.host == args.host)]

    if args.preview or args.id:
        want = args.preview or args.id
        hits = []
        for src in scoped:
            try:
                hits += [(src, i) for i in src.adapter.ids(src.root)
                         if i == want or (args.id and i.startswith(want))]
            except Exception as exc:  # noqa: BLE001 - one bad store mustn't hide the others
                print(safe_text(f"{src.tag}: could not read {src.root} "
                                f"({exc.__class__.__name__}: {exc})", line=True), file=sys.stderr)
        if not hits:
            sys.exit(f"no session id {'starts with' if args.id else 'is'} {want!r}")
        if len(hits) > 1 and args.id:
            print(f"{len(hits)} sessions match {want!r}:", file=sys.stderr)
            for src, i in hits[:10]:
                print(f"  {src.tag:<{SRCW}} {i}", file=sys.stderr)
            sys.exit(1)
        src, sid = hits[0]
        if args.id:
            print(sid)
            return
        rows, _, warns = load_rows([src], None, False, False, include_teams=True,
                                   include_scratch=True, include_empty=True, include_archived=True)
        for w in warns:
            print(safe_text(f"warning: {w}", line=True), file=sys.stderr)
        row = next((r for r in rows if r["id"] == sid), None)
        if row is None:
            sys.exit(f"session {sid} could not be read")
        print(preview(row, args.preview_width))
        return

    # Agent sessions and scheduled runs dominate some stores, so they are
    # hidden unless asked for. The picker loads them regardless, so its
    # toggles can reveal them without a rescan.
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.list
    load_skipped = interactive or args.teams or args.scheduled or args.background
    rows, total, warnings = load_rows(
        # Hidden sessions can be any age, so finding them means loading all.
        scoped, None if args.all or args.hidden else args.n, args.here, args.deep,
        include_teams=load_skipped, include_scratch=args.scratch, include_empty=args.empty,
        include_archived=args.archived,
        limit_counts_visible=load_skipped and not (args.teams and args.scheduled and args.background))
    for w in warnings:
        print(safe_text(f"warning: {w}", line=True), file=sys.stderr)
    if not rows:
        sys.exit("no sessions found")
    try:
        # Unreadable saved state stops here, before anything can save over it.
        load_hidden()
        load_review()
    except OSError as exc:
        sys.exit(f"hopback: cannot read {exc.filename}: {exc.strerror}")

    if not interactive:
        while STATE_PROBLEMS:
            print(safe_text(f"warning: {STATE_PROBLEMS.pop(0)}", line=True), file=sys.stderr)
        apply_review(rows, load_review())
        hid = load_hidden()
        rows = [r for r in rows if (hide_key(r) in hid) == args.hidden
                and (args.hidden or keep(r, args.teams, args.scheduled, args.background))]
        if text:
            rows = rank(rows, text)
        if not rows:
            sys.exit("no matching sessions")
        if args.print_cd:
            # Non-interactive resume: the newest match, printed, not run.
            # The output is read by a shell (cd "$(hopback --print-cd x)"), so it
            # is printed exactly as launch() built it: changing a path would cd
            # somewhere else. launch() refuses control characters instead.
            try:
                print(rows[0]["source"].launch(rows[0], args.yolo)[2])
            except RuntimeError as exc:
                sys.exit(safe_text(str(exc), line=True))
            return
        plain(rows, total, scoped)
        return

    chosen, yolo = pick(rows, text, args.yolo, total, args.here, scoped,
                        harness=harness, host=args.host, show_agents=args.teams,
                        show_scheduled=args.scheduled, show_leftovers=args.background,
                        warnings=warnings, start_hidden=args.hidden, start_review=args.review)
    if not chosen:
        return
    src = chosen["source"]
    try:
        argv, target, shown = src.launch(chosen, yolo)
    except RuntimeError as exc:
        sys.exit(safe_text(str(exc), line=True))

    # An inferred or missing path must not be guessed into: chdir'ing into the
    # wrong tree would resume the session against it. Stay put and say so.
    if not target or not Path(target).is_dir():
        print(safe_text(f"warning: {target or '(no directory)'} does not exist here; "
                        f"resuming from {Path.cwd()}", line=True), file=sys.stderr)
        target = str(Path.cwd())

    if args.print_cd:
        print(shown)
        return

    tag = "  [skip-permissions]" if yolo else ""
    print(safe_text(f"→ {chosen['name']}  ({src.label}){tag}", line=True), file=sys.stderr)
    if src.adapter.NAME == "claude" and src.host == this_host() and hasattr(os, "getuid"):
        tmp = Path(os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp") / f"claude-{os.getuid()}"
        bad = foreign_owned(tmp)
        if bad:
            conf = Path("/etc/tmpfiles.d/claude.conf")
            sys.exit(f"claude will refuse to start: these paths under {tmp} are not owned by you:\n  "
                     + "\n  ".join(bad[:20])
                     + (f"\n  ... and {len(bad) - 20} more" if len(bad) > 20 else "")
                     + "\nUsually a Docker bind mount recreated them as root. Fix: sudo rm -rf "
                       "the listed paths"
                     + (f", then run: sudo systemd-tmpfiles --create {conf}" if conf.exists() else ""))
    os.chdir(target)
    exec_agent(argv)


def exec_agent(argv):
    """Replace hopback with the agent. exec skips Python's exit handlers, so the
    preview pool's semaphores would stay registered with multiprocessing's
    resource tracker; it outlives the exec and, when the agent exits, prints
    "There appear to be 5 leaked semaphore objects". Run multiprocessing's own
    exit handler first, which unlinks them and lets the tracker go quietly."""
    from multiprocessing import util
    util._exit_function()  # pyright: ignore[reportAttributeAccessIssue]
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        sys.exit(f"could not run {argv[0]}: {exc}")


def foreign_owned(root, budget=1.0):
    """Paths under `root` (or root itself) not owned by this user; Claude Code
    refuses to start, with a generic error, when its /tmp directory has any.
    Symlinks are not followed. A missing root means nothing to report, and a
    walk that takes longer than `budget` seconds is abandoned rather than
    holding up the resume."""
    uid, out = os.getuid(), []
    try:
        if root.lstat().st_uid != uid:
            return [str(root)]
    except OSError:
        return []      # missing or unreadable: nothing this check can say
    deadline = time.monotonic() + budget
    # Only an owner mismatch is reported. An unreadable directory the user
    # owns, or one a running session deletes mid-walk, is not this problem.
    for dirpath, dirs, files in os.walk(root, onerror=lambda e: None):
        for name in dirs + files:
            if time.monotonic() > deadline:
                return out
            path = os.path.join(dirpath, name)
            try:
                if os.lstat(path).st_uid != uid:
                    out.append(path)
            except OSError:
                pass
    return out


def run():
    try:
        main()
    except BrokenPipeError:
        os._exit(0)
    except KeyboardInterrupt:
        sys.exit(130)

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
all so nothing can highlight on hover. Textual's CSS cascade lets the focus rule
override the stripe rule, and gives real :hover.

Usage:
    hopback                       every session, every harness, every host
    hopback codex                 one harness (claude, codex, hermes)
    hopback --host win            one host (wsl, win, linux, mac)
    hopback codex stremio         harness plus a pre-filter
    hopback -y                    resume skipping permission prompts
    hopback -l                    plain list, no picker (also auto when piped)
    hopback -n 50 / -a            how many to load per store (default 300) / all
    hopback -d                    only sessions from the current directory
    hopback -t / -r               include agent sessions / scheduled runs
    hopback -s / -e / --archived  include /tmp, empty, archived sessions
    hopback --id <prefix>         print the full id for a prefix, then exit
    hopback --print-cd            print the command instead of running it
    hopback --doctor              show every store found and how it resumes

In the picker:
    type to search (space-separated words must all match), arrows to move
    ENTER      resume                 CTRL-Y   resume skipping permissions
    TAB        next harness tab       CTRL-O   cycle hosts
    CTRL-T     agent sessions         CTRL-R   scheduled runs
    CTRL-/     preview pane           CTRL-U   clear search      ESC quit
"""
import argparse
import os
import sys
import time
from pathlib import Path

from .adapters import AGENT, BY_NAME, LEFTOVER, ROLES, SCHEDULED
from .fmt import size_str, when
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
            got, n = src.adapter.collect(src.root, limit, here_only, deep, **opts)
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


def preview(row, width=80):
    """The preview pane for one row, the same shape for every harness."""
    src = row["source"]
    try:
        d = src.adapter.details(src.root, row["id"])
    except Exception as exc:  # noqa: BLE001
        return f"could not read this session: {exc.__class__.__name__}: {exc}"
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
    return "\n".join(out)


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
    if len(shown) > namew:
        shown = shown[: namew - 1] + "…"
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
        print(fmt(r, namew, home, now) + f'  {r["id"]}')
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


# --------------------------------------------------------------------------
# Textual UI
# --------------------------------------------------------------------------

def build_app(rows, query, yolo, total, here_only, sources, harness=None, host=None,
              show_agents=False, show_scheduled=False, show_leftovers=False, warnings=()):
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Input, ListItem, ListView, Static, Tab, Tabs

    harnesses = [a for a in BY_NAME if any(s.adapter.NAME == a for s in sources)]
    hosts = sorted({s.host for s in sources}, key=lambda h: (h != this_host(), h))

    class SessionList(ListView):
        """ListView whose wheel changes the SELECTION rather than the viewport.

        Scrolling the viewport under a stationary cursor means the highlighted
        row silently becomes one you are not pointing at; moving the selection
        keeps the wheel and the highlight talking about the same thing.
        """

        def on_mouse_scroll_down(self, event):
            event.prevent_default()
            event.stop()
            self.action_cursor_down()

        def on_mouse_scroll_up(self, event):
            event.prevent_default()
            event.stop()
            self.action_cursor_up()

    class Row(ListItem):
        """One session. Carries its own alternation class so the CSS cascade,
        not the text, decides its colour; that is what lets the focus and hover
        rules override the stripe, which ANSI in fzf could never do."""

        def __init__(self, row, text, alt):
            super().__init__(Static(text, markup=False))
            self.row = row
            if alt:
                self.add_class("alt")

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
        #tabs { height: 2; background: #16161e; }
        #tabs Tab { color: #565f89; padding: 0 2; }
        #tabs Tab.-active { color: #ffc896; text-style: bold; }
        #head { color: #7dcfff; height: auto; }
        #warn { color: #f7768e; height: auto; }
        .control { color: #e0af68; height: 1; width: auto; margin-right: 4; }
        .control:hover { background: #2f3549; color: #ffc896; }
        #controls { height: 1; padding-left: 2; }
        #keys { height: auto; }
        #labels { color: #7dcfff; height: 1; }
        #list { background: #16161e; height: 1fr; scrollbar-size-vertical: 1; }
        #list > ListItem { background: #16161e; color: #a9b1d6; padding: 0 1; }
        /* Alternating rows. A later, equally specific rule still loses to the
           more specific state selectors below, which is the whole point. */
        #list > ListItem.alt { background: #262b3d; }
        #list > ListItem.hovered,
        #list > ListItem.-hovered,
        #list > ListItem.alt.hovered,
        #list > ListItem.alt.-hovered { background: #343b58; color: #c0caf5; }
        #list > ListItem.-highlight,
        #list > ListItem.alt.-highlight,
        #list > ListItem.hovered.-highlight,
        #list > ListItem.-hovered.-highlight,
        #list > ListItem.alt.hovered.-highlight,
        #list > ListItem.alt.-hovered.-highlight {
            background: #3b4261; color: #ffc896; text-style: bold;
        }
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
            self._hover_idx = None

        def compose(self) -> ComposeResult:
            with Vertical(id="frame") as frame:
                frame.border_title = "  hopback  "
                yield Input(placeholder="search", id="search", value=self.search_text)
                yield Tabs(Tab("All", id="t-all"),
                           *(Tab(BY_NAME[h].LABEL, id=f"t-{h}") for h in harnesses),
                           id="tabs", active=f"t-{self.harness or 'all'}")
                yield Static("", id="head", markup=False)
                yield Static("\n".join(warnings), id="warn", markup=False,
                             classes="" if warnings else "hidden")
                with Horizontal(id="controls"):
                    yield Clickable("cycle_host", id="hosts", classes="control")
                    yield Clickable("toggle_agents", id="toggle", classes="control")
                    yield Clickable("toggle_scheduled", id="sched", classes="control")
                    yield Clickable("toggle_leftovers", id="left", classes="control")
                yield Static("", id="keys")
                yield Static("", id="labels", markup=False)
                yield SessionList(id="list")
                with VerticalScroll(id="preview"):
                    yield Static("", id="preview_body", markup=False)

        async def on_mount(self):
            await self.refresh_rows()
            self.query_one("#search", Input).focus()

        # -- data ------------------------------------------------------------
        def in_scope(self, r, ignore_harness=False):
            """Host, harness and toggles, but not the search text."""
            src = r["source"]
            if self.host and src.host != self.host:
                return False
            if not ignore_harness and self.harness and src.adapter.NAME != self.harness:
                return False
            return keep(r, self.show_agents, self.show_scheduled, self.show_leftovers)

        def visible_rows(self):
            rs = [r for r in self.all_rows if self.in_scope(r)]
            if self.search_text:
                rs = rank(rs, self.search_text)
            return rs

        async def refresh_rows(self, keep_index=True):
            lv = self.query_one("#list", ListView)
            prev = lv.index if keep_index else 0
            self._visible = self.visible_rows()
            home = str(Path.home())
            now = time.time()
            namew = min(max((len(r["name"]) for r in self._visible), default=4), 34)
            self._hover_idx = None
            await lv.clear()
            items = [Row(r, fmt(r, namew, home, now), i % 2 == 1)
                     for i, r in enumerate(self._visible)]
            if items:
                await lv.extend(items)
                lv.index = min(prev or 0, len(items) - 1)
            self.update_header(namew)
            self.update_preview()

        def update_header(self, namew):
            shown = len(self._visible)
            scoped = [r for r in self.all_rows if self.in_scope(r, ignore_harness=True)]
            tabs = self.query_one("#tabs", Tabs)
            tabs.query_one("#t-all", Tab).label = f"All {len(scoped):,}"
            for h in harnesses:
                n = sum(1 for r in scoped if r["source"].adapter.NAME == h)
                tabs.query_one(f"#t-{h}", Tab).label = f"{BY_NAME[h].LABEL} {n:,}"
            scope = ("this directory only" if here_only
                     else f"all directories, {total:,} on disk")
            self.query_one("#head", Static).update(
                f"  {shown:,} sessions · {scope} · newest first\n"
                f"  ENTER resume    CTRL-Y resume skipping permissions    TAB harness    "
                f"CTRL-/ preview    ESC quit")
            self.head_text = f"{shown} sessions"

            per_host = {h: sum(1 for r in self.all_rows if r["source"].host == h
                               and keep(r, self.show_agents, self.show_scheduled, self.show_leftovers)
                               and (not self.harness or r["source"].adapter.NAME == self.harness))
                        for h in hosts}
            counts = " · ".join(f"{HOSTS.get(h, h)} {per_host[h]:,}" for h in hosts)
            self.host_text = (f"[ CTRL-O ] hosts: {HOSTS[self.host] if self.host else 'all'}  ({counts})")
            self.query_one("#hosts", Static).update(self.host_text)

            def hidden(roles):
                return sum(1 for r in self.all_rows if r.get("role") in roles
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
            sched.set_class(n_sched == 0, "hidden")
            n_left = hidden(LEFTOVER)
            self.left_text = (f"[ CTRL-B ] job leftovers "
                              f"{'shown' if self.show_leftovers else 'hidden'} ({n_left:,})")
            left = self.query_one("#left", Static)
            left.update(self.left_text)
            left.set_class(n_left == 0, "hidden")

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
            pane = self.query_one("#preview_body", Static)
            lv = self.query_one("#list", ListView)
            if not self._visible or lv.index is None:
                pane.update("")
                return
            row = self._visible[min(lv.index, len(self._visible) - 1)]
            pane.update(preview(row, max(40, self.size.width - 6)))

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
            if want != self.harness:
                self.harness = want
                self.run_worker(self.refresh_rows(keep_index=False), exclusive=True)

        def on_list_view_highlighted(self, event):
            self.update_preview()

        def on_list_view_selected(self, event):
            self.action_resume()

        def on_mouse_move(self, event):
            """Hover highlighting for rows.

            ListView absorbs pointer events, so neither :hover nor Enter/Leave
            ever reaches a row. Screen coordinates are mapped to a row index
            instead: rows are one line tall, so the arithmetic is exact.
            """
            lv = self.query_one("#list", ListView)
            region = lv.content_region
            kids = list(lv.children)
            idx = None
            if (region.x <= event.screen_x < region.x + region.width
                    and region.y <= event.screen_y < region.y + region.height):
                idx = event.screen_y - region.y + int(lv.scroll_offset.y)
                if not (0 <= idx < len(kids)):
                    idx = None
            if idx == self._hover_idx:
                return
            if self._hover_idx is not None and self._hover_idx < len(kids):
                kids[self._hover_idx].remove_class("hovered")
            self._hover_idx = idx
            if idx is not None:
                kids[idx].add_class("hovered")

        # -- actions ---------------------------------------------------------
        def action_cursor_down(self):
            self.query_one("#list", ListView).action_cursor_down()

        def action_cursor_up(self):
            self.query_one("#list", ListView).action_cursor_up()

        def action_page_down(self):
            lv = self.query_one("#list", ListView)
            for _ in range(10):
                lv.action_cursor_down()

        def action_page_up(self):
            lv = self.query_one("#list", ListView)
            for _ in range(10):
                lv.action_cursor_up()

        def action_clear_query(self):
            self.query_one("#search", Input).value = ""

        def action_cycle_tab(self, step):
            order = [None] + harnesses
            nxt = order[(order.index(self.harness) + step) % len(order)]
            # Setting the active tab fires TabActivated, which refreshes.
            self.query_one("#tabs", Tabs).active = f"t-{nxt or 'all'}"

        def action_cycle_host(self):
            order = [None] + hosts
            self.host = order[(order.index(self.host) + 1) % len(order)]
            self.run_worker(self.refresh_rows(keep_index=False), exclusive=True)

        def action_toggle_agents(self):
            self.show_agents = not self.show_agents
            self.run_worker(self.refresh_rows(keep_index=False), exclusive=True)

        def action_toggle_scheduled(self):
            self.show_scheduled = not self.show_scheduled
            self.run_worker(self.refresh_rows(keep_index=False), exclusive=True)

        def action_toggle_leftovers(self):
            self.show_leftovers = not self.show_leftovers
            self.run_worker(self.refresh_rows(keep_index=False), exclusive=True)

        def action_toggle_preview(self):
            self.query_one("#preview").toggle_class("hidden")

        def action_resume(self, yolo=False):
            lv = self.query_one("#list", ListView)
            if not self._visible or lv.index is None:
                return
            self.result = (self._visible[min(lv.index, len(self._visible) - 1)],
                           self.yolo or yolo)
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
                    help="how many sessions to load per store (default 300)")
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
            except Exception:  # noqa: BLE001
                continue
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
        rows, _, _ = load_rows([src], None, False, False, include_teams=True,
                               include_scratch=True, include_empty=True, include_archived=True)
        row = next((r for r in rows if r["id"] == sid), None)
        if row is None:
            sys.exit(f"session {sid} could not be read")
        print(preview(row, args.preview_width))
        return

    # Agent sessions and scheduled runs dominate some stores, so they are
    # hidden unless asked for. The picker loads them regardless, so its
    # toggles can reveal them without a rescan.
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.list
    load_hidden = interactive or args.teams or args.scheduled or args.background
    rows, total, warnings = load_rows(
        scoped, None if args.all else args.n, args.here, args.deep,
        include_teams=load_hidden, include_scratch=args.scratch, include_empty=args.empty,
        include_archived=args.archived,
        limit_counts_visible=load_hidden and not (args.teams and args.scheduled and args.background))
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if not rows:
        sys.exit("no sessions found")

    if not interactive:
        rows = [r for r in rows if keep(r, args.teams, args.scheduled, args.background)]
        if text:
            rows = rank(rows, text)
        if not rows:
            sys.exit("no matching sessions")
        if args.print_cd:
            # Non-interactive resume: the newest match, printed, not run.
            print(rows[0]["source"].launch(rows[0], args.yolo)[2])
            return
        plain(rows, total, scoped)
        return

    chosen, yolo = pick(rows, text, args.yolo, total, args.here, scoped,
                        harness=harness, host=args.host, show_agents=args.teams,
                        show_scheduled=args.scheduled, show_leftovers=args.background,
                        warnings=warnings)
    if not chosen:
        return
    src = chosen["source"]
    try:
        argv, target, shown = src.launch(chosen, yolo)
    except RuntimeError as exc:
        sys.exit(str(exc))

    # An inferred or missing path must not be guessed into: chdir'ing into the
    # wrong tree would resume the session against it. Stay put and say so.
    if not target or not Path(target).is_dir():
        print(f"warning: {target or '(no directory)'} does not exist here; "
              f"resuming from {Path.cwd()}", file=sys.stderr)
        target = str(Path.cwd())

    if args.print_cd:
        print(shown)
        return

    tag = "  [skip-permissions]" if yolo else ""
    print(f"→ {chosen['name']}  ({src.label}){tag}", file=sys.stderr)
    os.chdir(target)
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        sys.exit(f"could not run {argv[0]}: {exc}")


def run():
    try:
        main()
    except BrokenPipeError:
        os._exit(0)
    except KeyboardInterrupt:
        sys.exit(130)

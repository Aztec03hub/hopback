"""hopback: browse AI coding-agent sessions across ALL directories, then resume one.

Phase 1 supports Claude Code only, through hopback/adapters/claude.py. Each
session comes from a Source (host, harness, store root); see sources.py.

`claude --resume` only offers sessions for the current working directory, and
`claude agents` lists background sessions only. There is no built-in way to see
everything, so this reads the session store directly:

    ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl

Selecting a session chdirs to that session's own recorded directory and execs
`claude --resume <id>` there, so the resumed session gets the working directory
it actually ran in rather than wherever you happened to be standing.

Name resolution, in order: the /rename title (customTitle), then the generated
title (aiTitle), then the assigned agent name, then "-". Titles are rewritten
repeatedly through a session, so the LAST occurrence wins and files are read
backwards to find it cheaply. A long session is tens of megabytes; reading
forward would make this unusable across thousands of files.

The UI is Textual rather than fzf. fzf applies its focus colours only where the
row text does not already set its own, so alternating row colours and a uniform
focused row are mutually exclusive there, and it has no mouse-motion events at
all so nothing can highlight on hover. Textual's CSS cascade lets the focus rule
override the stripe rule, and gives real :hover. Everything else is deliberately
identical to the fzf version it replaces.

Usage:
    claude-sessions                 picker
    claude-sessions stremio         picker, pre-filtered
    claude-sessions -y              resume with --dangerously-skip-permissions
    claude-sessions -l              plain list, no picker (also auto when piped)
    claude-sessions -n 50 / -a      how many to load (default 300)
    claude-sessions -d              only sessions from the current directory
    claude-sessions -t              include agent sessions (hidden by default)
    claude-sessions -s              include /tmp sessions
    claude-sessions -e              include sessions with no messages
    claude-sessions --id <prefix>   print the full id for a prefix, then exit
    claude-sessions --print-cd      print the shell command instead of running it

In the picker:
    type to search, arrows or ctrl-j/k to move
    ENTER      resume
    CTRL-Y     resume with --dangerously-skip-permissions
    CTRL-T     show/hide agent sessions (or click the toggle line)
    CTRL-/     toggle the preview pane
    CTRL-U     clear the search
    ESC        quit
"""
import argparse
import os
import shlex
import sys
import time
from pathlib import Path

from .fmt import size_str, when
from .sources import SRCW, discover


def load_rows(sources, limit, here_only, deep, **opts):
    """Every source's rows, tagged with their Source, newest first."""
    rows, total = [], 0
    for src in sources:
        got, n = src.adapter.collect(src.root, limit, here_only, deep, **opts)
        for r in got:
            r["source"] = src
        rows += got
        total += n
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows, total


def preview(row, width):
    src = row["source"]
    return src.adapter.preview_text(src.root, row["id"], width)


def fmt(row, namew, home, now):
    shown = row["name"]
    if len(shown) > namew:
        shown = shown[: namew - 1] + "…"
    mark = "·" if row["named"] else " "
    path = row["cwd"].replace(home, "~", 1) + ("?" if row["guessed"] else "")
    if len(path) > 44:
        path = "…" + path[-43:]
    badge = {"lead": "lead", "team": "team", "sdk": "sdk"}.get(row.get("role", ""), "")
    return (f'{when(row["mtime"], now):>20}  {row["source"].tag:<{SRCW}} {badge:<4} '
            f'{mark}{shown:<{namew}}  {path:<44}  {size_str(row["bytes"]):>8}')


def plain(rows, total):
    home = str(Path.home())
    now = time.time()
    namew = min(max((len(r["name"]) for r in rows), default=4), 40)
    for r in rows:
        print(fmt(r, namew, home, now) + f'  {r["id"]}')
    print(f'\n{len(rows)} shown, {total} sessions total'
          f'   · = named with /rename, ? = path inferred', file=sys.stderr)
    print("resume:  claude --resume <session-id>", file=sys.stderr)


def fuzzy(needle, haystack):
    """fzf-style subsequence match, case-insensitive. Returns True on a match."""
    if not needle:
        return True
    it = iter(haystack.lower())
    return all(ch in it for ch in needle.lower())


# --------------------------------------------------------------------------
# Textual UI
# --------------------------------------------------------------------------

def build_app(rows, query, yolo, total, here_only):
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical, VerticalScroll
    from textual.widgets import Input, ListItem, ListView, Static

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
        #head { color: #7dcfff; height: auto; }
        #toggle { color: #e0af68; height: 1; }
        #toggle:hover { background: #2f3549; color: #ffc896; }
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
            self.query = query or ""
            self.yolo = yolo
            self.show_agents = False
            self.result = None
            self._visible = []
            self.toggle_text = ""
            self.head_text = ""
            self._hover_idx = None

        def compose(self) -> ComposeResult:
            with Vertical(id="frame") as frame:
                frame.border_title = "  hopback  "
                yield Input(placeholder="search", id="search", value=self.query)
                yield Static("", id="head", markup=False)
                yield Static("", id="toggle", markup=False)
                yield Static("", id="keys")
                yield Static("", id="labels", markup=False)
                yield SessionList(id="list")
                with VerticalScroll(id="preview"):
                    yield Static("", id="preview_body", markup=False)

        async def on_mount(self):
            await self.refresh_rows()
            self.query_one("#search", Input).focus()

        # -- data ------------------------------------------------------------
        def visible(self):
            rs = self.all_rows
            if not self.show_agents:
                rs = [r for r in rs if r.get("role") not in ("team", "sdk")]
            if self.query:
                rs = [r for r in rs
                      if fuzzy(self.query, f'{r["name"]} {r["cwd"]} {r["id"]}')]
            return rs

        async def refresh_rows(self, keep_index=True):
            lv = self.query_one("#list", ListView)
            prev = lv.index if keep_index else 0
            self._visible = self.visible()
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
            hidden = sum(1 for r in self.all_rows
                         if r.get("role") in ("team", "sdk")) if not self.show_agents else 0
            teams = sum(1 for r in self.all_rows if r.get("role") == "team")
            sdks = sum(1 for r in self.all_rows if r.get("role") == "sdk")
            breakdown = " + ".join(x for x in (f"{teams} teammate" if teams else "",
                                               f"{sdks} SDK" if sdks else "") if x)
            scope = ("this directory only" if here_only
                     else f"all directories, {total} on disk")
            self.query_one("#head", Static).update(
                f"  {shown} sessions · {scope} · newest first\n"
                f"  ENTER resume    CTRL-Y resume skipping permissions    "
                f"CTRL-/ preview    ESC quit")
            self.toggle_text = (
                f"  [ CLICK OR CTRL-T TO HIDE ]  agent sessions shown  "
                f"({breakdown or 'none'})"
                if self.show_agents else
                f"  [ CLICK OR CTRL-T TO SHOW ]  {hidden} agent sessions hidden  "
                f"({breakdown or 'none'})")
            # Kept as attributes as well as rendered, so a test can assert what
            # the header says without reaching into Textual's internals.
            self.head_text = f"{shown} sessions"
            self.query_one("#toggle", Static).update(self.toggle_text)
            def chip(tok, desc):
                return f"[b #1a1b26 on #7dcfff] {tok} [/][#565f89]  {desc}[/]"
            self.query_one("#keys", Static).update(
                "  " + "   ".join((chip("lead", "led an agent team"),
                                   chip("team", "was a teammate"),
                                   chip("sdk", "started by the Agent SDK"))) + "\n"
                "  " + "   ".join((chip("·", "you named it with /rename"),
                                   chip("?", "directory inferred, may be wrong"))))
            self.query_one("#labels", Static).update(
                f'  {"LAST ACTIVE":>20}  {"SOURCE":<{SRCW}} {"ROLE":<4} {"":1}{"NAME":<{namew}}  '
                f'{"DIRECTORY":<44}  {"SIZE":>8}')

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
            self.query = event.value
            await self.refresh_rows(keep_index=False)

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

        def on_click(self, event):
            widget = getattr(event, "widget", None)
            if widget is not None and getattr(widget, "id", None) == "toggle":
                self.action_toggle_agents()

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

        def action_toggle_agents(self):
            self.show_agents = not self.show_agents
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


def pick(rows, query, yolo, total, here_only):
    """Run the picker. Returns (row, yolo) or (None, yolo)."""
    try:
        app = build_app(rows, query, yolo, total, here_only)
    except ImportError as exc:
        print(f"textual is not available: {exc}", file=sys.stderr)
        return None, yolo
    app.run()
    if app.result:
        return app.result
    return None, yolo


def main():
    ap = argparse.ArgumentParser(
        prog="hopback",
        description="Browse and resume AI coding-agent sessions from any directory.",
    )
    ap.add_argument("filter", nargs="?", help="pre-filter on name, path or id")
    ap.add_argument("-n", type=int, default=300, metavar="N",
                    help="how many sessions to load (default 300)")
    ap.add_argument("-a", "--all", action="store_true", help="load every session")
    ap.add_argument("-d", "--here", action="store_true", help="only the current directory")
    ap.add_argument("-e", "--empty", action="store_true",
                    help="include sessions that contain no messages at all")
    ap.add_argument("-s", "--scratch", action="store_true",
                    help="include sessions rooted in /tmp (hidden by default)")
    ap.add_argument("-t", "--teams", action="store_true",
                    help="include agent sessions (hidden by default; CTRL-T in the picker)")
    ap.add_argument("-l", "--list", action="store_true", help="print a list, no picker")
    ap.add_argument("-y", "--yolo", "--dangerous", dest="yolo", action="store_true",
                    help="resume skipping permission prompts (claude: --dangerously-skip-permissions)")
    ap.add_argument("--print-cd", action="store_true",
                    help="print the shell command for the pick instead of running it")
    ap.add_argument("--id", metavar="PREFIX", help="resolve an id prefix and exit")
    ap.add_argument("--deep", action="store_true",
                    help="read whole files; slower, finds titles set only at the start")
    ap.add_argument("--doctor", action="store_true",
                    help="report why the picker did or did not appear")
    ap.add_argument("--preview", metavar="ID", help=argparse.SUPPRESS)
    ap.add_argument("--preview-width", type=int, default=80, help=argparse.SUPPRESS)
    args = ap.parse_args()

    sources = discover()
    if not sources:
        sys.exit("no session store found (looked for ~/.claude/projects)")

    if args.doctor:
        # For "I ran it and nothing happened": show every condition that decides
        # between picker, list, and silent exit.
        try:
            import textual
            tv = textual.__version__
        except ImportError:
            tv = "NOT INSTALLED"
        print(f"script      {os.path.abspath(__file__)}")
        print(f"python      {sys.version.split()[0]}  ({sys.executable})")
        print(f"textual     {tv}")
        print(f"stdin tty   {sys.stdin.isatty()}")
        print(f"stdout tty  {sys.stdout.isatty()}")
        print(f"TERM        {os.environ.get('TERM', '(unset)')}")
        for src in sources:
            print(f"source      {src.tag:<{SRCW}} {src.root}  sessions={src.adapter.count(src.root)}")
        mode = "picker" if sys.stdin.isatty() and sys.stdout.isatty() else "list"
        print(f"\nwould use   {mode}")
        if mode == "list":
            print("  (not a terminal on both ends: are you running this through a"
                  " pipe, or from inside another program?)")
        return

    if args.preview:
        for src in sources:
            if args.preview in src.adapter.ids(src.root):
                print(src.adapter.preview_text(src.root, args.preview, args.preview_width))
                return
        sys.exit(f"no session {args.preview!r}")

    if args.id:
        hits = [i for src in sources for i in src.adapter.ids(src.root)
                if i.startswith(args.id)]
        if not hits:
            sys.exit(f"no session id starts with {args.id!r}")
        if len(hits) > 1:
            print(f"{len(hits)} sessions match {args.id!r}:", file=sys.stderr)
            for h in hits[:10]:
                print(f"  {h}", file=sys.stderr)
            sys.exit(1)
        print(hits[0])
        return

    # Agent sessions are spawned workers rather than sessions you started, and
    # they dominate the store, so they are hidden unless asked for. The picker
    # loads them regardless so the toggle can reveal them without a re-scan.
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.list
    load_teams = args.teams or interactive
    rows, total = load_rows(sources, None if args.all else args.n, args.here, args.deep,
                            include_teams=load_teams, include_scratch=args.scratch,
                            include_empty=args.empty,
                            limit_counts_visible=load_teams and not args.teams)
    if not rows:
        sys.exit("no sessions found")

    if not interactive:
        if args.filter:
            q = args.filter.lower()
            rows = [r for r in rows if q in f'{r["name"]} {r["cwd"]} {r["id"]}'.lower()]
            if not rows:
                sys.exit("no matching sessions")
        plain(rows, total)
        return

    chosen, yolo = pick(rows, args.filter, args.yolo, total, args.here)
    if not chosen:
        return

    cmd = chosen["source"].adapter.resume_cmd(chosen, yolo)
    target = chosen["cwd"]

    # An inferred path may not exist, and chdir'ing into a guess would resume the
    # session against the wrong tree. Stay put and say so instead.
    if not Path(target).is_dir():
        print(f"warning: {target} does not exist; resuming from {Path.cwd()}",
              file=sys.stderr)
        target = str(Path.cwd())

    if args.print_cd:
        print(f"cd {shlex.quote(target)} && {' '.join(shlex.quote(c) for c in cmd)}")
        return

    tag = "  [skip-permissions]" if yolo else ""
    print(f"→ {chosen['name']}  ({target}){tag}", file=sys.stderr)
    os.chdir(target)
    try:
        os.execvp(cmd[0], cmd)
    except OSError as exc:
        sys.exit(f"could not run {cmd[0]}: {exc}")


def run():
    try:
        main()
    except BrokenPipeError:
        os._exit(0)
    except KeyboardInterrupt:        sys.exit(130)

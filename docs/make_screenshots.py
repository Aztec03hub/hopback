"""Regenerate the README screenshots from a FAKE session store.

Never point this at a real home: the screenshots are public. It builds the
invented multi-harness, multi-host store in tests/demo_store.py, drives the
picker headlessly with Textual's test pilot, and saves SVGs next to this file.

    python -m venv /tmp/v && /tmp/v/bin/pip install .
    /tmp/v/bin/python docs/make_screenshots.py
    # then render each SVG to PNG at its native size (see README)
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "tests"))

# Same length as "/home/dev" so the substitution below keeps SVG text widths.
FAKE_HOME = Path("/tmp/csdm")
FAKE_WIN = Path("/tmp/cswn")
os.environ["HOME"] = str(FAKE_HOME)
os.environ["HOPBACK_WINDOWS_HOME"] = str(FAKE_WIN)
os.environ["XDG_STATE_HOME"] = str(FAKE_HOME / ".state")  # not the real hidden list

import demo_store  # noqa: E402
from hopback import cli  # noqa: E402
from hopback.sources import discover  # noqa: E402


def scrub(out):
    svg = out.read_text()
    svg = svg.replace(str(FAKE_HOME), "/home/dev")
    # Crop away the fake window chrome (title bar, traffic lights, frame) by
    # pointing the viewBox at the terminal area alone.
    m = re.search(r'clip-terminal">\s*<rect x="0" y="0" width="([\d.]+)" height="([\d.]+)"', svg)
    t = re.search(r'<g transform="translate\(([\d.]+), ([\d.]+)\)" '
                  r'clip-path="url\(#[^)]*clip-terminal\)">', svg)
    if m and t:
        w, h = float(m[1]), float(m[2])
        svg = re.sub(r'viewBox="[^"]*"', f'viewBox="{t[1]} {t[2]} {w:.1f} {h:.1f}"', svg, count=1)
        svg = svg.replace('<svg class="rich-terminal"',
                          f'<svg width="{w:.0f}" height="{h:.0f}" class="rich-terminal"', 1)
    out.write_text(svg)
    print("wrote", out)


def select(app, pred):
    lv = app.query_one("#list")
    lv.index = next(i for i, r in enumerate(app._visible) if pred(r))


async def rebuilt(app, pilot):
    """Wait for the newest list rebuild. Not app.workers.wait_for_complete(): it
    gathers the preview worker too, which the app cancels itself, and the
    gather then raises WorkerCancelled."""
    for _ in range(500):
        await pilot.pause(0.01)
        if app._shown_ticket == app._refresh_ticket:
            return
    raise AssertionError("rebuild never finished")


async def shoot(name, keys=(), query="", hover=None, pick=None):
    sources = discover()
    rows, total, _ = cli.load_rows(sources, 300, False, False, include_teams=True,
                                   include_scratch=True, limit_counts_visible=True)
    app = cli.build_app(rows, query, False, total, False, sources)
    async with app.run_test(size=(140, 62)) as pilot:
        await pilot.pause()
        for k in keys:
            await pilot.press(k)
            await rebuilt(app, pilot)
            await pilot.pause()
        if pick:
            select(app, pick)
        await rebuilt(app, pilot)
        if hover:
            await pilot.hover("#list", offset=hover)
        await pilot.pause(0.3)
        out = HERE / f"{name}.svg"
        app.save_screenshot(filename=out.name, path=str(HERE))
        scrub(out)


def shoot_list(name):
    """`hopback -l -t -r`, rendered the way a terminal shows it."""
    from rich.console import Console
    from rich.text import Text
    res = subprocess.run([sys.executable, "-m", "hopback", "-l", "-t", "-r", "-s"],
                         cwd=str(HERE.parent), capture_output=True, text=True, env=os.environ)
    if res.returncode or "Traceback" in res.stderr:
        sys.exit(f"hopback -l failed, not saving a screenshot of it:\n{res.stderr}")
    con = Console(record=True, width=176, file=open(os.devnull, "w"))
    con.print(Text("$ hopback -l -t -r", style="bold #7aa2f7"))
    con.print(Text(res.stdout.rstrip()))
    con.print(Text(res.stderr.rstrip(), style="#565f89"))
    out = HERE / f"{name}.svg"
    out.write_text(con.export_svg(title="hopback -l"))
    scrub(out)


async def main():
    # Here, not at import: the picker's preview workers re-import this module,
    # and wiping the store from inside one of them empties it mid-run.
    for d in (FAKE_HOME, FAKE_WIN):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()
    ids = demo_store.build(FAKE_HOME, FAKE_WIN)
    # Everything, every harness and host; a Claude session with cost selected,
    # the mouse hovering a Codex row.
    await shoot("picker", keys=("down",), hover=(30, 3))
    # The Codex tab: one harness only, a Codex session's preview.
    await shoot("harness-tab", keys=("tab", "tab"),
                pick=lambda r: r["id"] == ids["codex-cli-user"])
    # CTRL-O to Windows: sessions from the Windows side, resumed via PowerShell.
    await shoot("windows", keys=("ctrl+o", "ctrl+o"),
                pick=lambda r: r["id"] == ids["claude-win"])
    # CTRL-T and CTRL-R: agent sessions and scheduled runs revealed.
    await shoot("agents", keys=("ctrl+t", "ctrl+r"),
                pick=lambda r: r["id"] == ids["claude-team"])
    # CTRL-X hid two sessions; the hidden view lists them with their harness.
    await shoot("hidden", keys=("ctrl+x", "down", "ctrl+x", "ctrl+g"))
    # Search ANDs words and matches the source too.
    await shoot("search", keys=("ctrl+t",), query="acme-api",
                pick=lambda r: r["id"] == ids["hermes-d-subagent"])
    shoot_list("plain-list")
    for d in (FAKE_HOME, FAKE_WIN):
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

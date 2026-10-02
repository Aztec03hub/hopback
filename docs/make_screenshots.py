"""Regenerate the README screenshots from a FAKE session store.

Never point this at a real ~/.claude: the screenshots are public. It builds a
throwaway HOME with invented sessions, drives the picker headlessly with
Textual's test pilot, and saves SVGs next to this file.

    python3 -m venv /tmp/v && /tmp/v/bin/pip install textual
    /tmp/v/bin/python docs/make_screenshots.py
"""
import asyncio
import json
import re
import os
import shutil
import subprocess
import sys
import time
import types
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Same length as "/home/dev" so the substitution below keeps SVG text widths.
fake_home = Path("/tmp/csdm")
shutil.rmtree(fake_home, ignore_errors=True)
fake_home.mkdir()
os.environ["HOME"] = str(fake_home)  # before import: the module reads Path.home()

NOW = time.time()
H = 3600
# (age seconds, cwd under ~, title, renamed, first prompt, last prompt, reply, kind, branch, cost, size pad KB)
SESSIONS = [
    (4 * 60, "projects/acme-api", "Rate limiter for public API", True,
     "Add per-key rate limiting to the public API", "Now add a Redis backend for the limiter",
     "Done. The limiter now uses Redis with a sliding window; 14 tests pass.", "live", "feat/rate-limit", 3.42, 900),
    (50 * 60, "projects/acme-web", "Dark mode toggle", False,
     "Add a dark mode toggle to the settings page", "Make it follow the system theme by default",
     "It now reads prefers-color-scheme on first load and remembers overrides.", "cli", "main", 1.18, 300),
    (3 * H, "projects/acme-api", "Migrate auth to OAuth 2.1", False,
     "Plan the OAuth 2.1 migration", "Write the migration guide for clients",
     "Guide written to docs/oauth-migration.md.", "lead", "oauth21", 8.90, 6200),
    (3 * H + 200, "projects/acme-api", "", False, "Review token refresh", "",
     "Refresh path verified.", "team", "", 0, 120),
    (3 * H + 400, "projects/acme-api", "", False, "Audit scopes", "",
     "Scopes audited.", "team", "", 0, 140),
    (5 * H, "dotfiles", "Tidy zsh config", True,
     "Clean up my zshrc", "Split aliases into their own file",
     "Aliases moved to ~/.zsh/aliases.zsh.", "cli", "master", 0.37, 60),
    (26 * H, "projects/ml-notebook", "Feature importance plots", False,
     "Plot SHAP values for the churn model", "Export the plots as PNG",
     "Saved four plots to figures/.", "cli", "exp/shap", 2.05, 2400),
    (27 * H, "projects/ml-notebook", "Nightly eval run", False,
     "Run the eval suite", "", "Eval complete.", "sdk", "", 0, 200),
    (3 * 24 * H, "projects/acme-web", "Fix flaky checkout test", False,
     "The checkout e2e test fails one run in five", "Is it the network mock?",
     "Yes: the mock resolved before the route registered. Fixed with an await.", "cli", "fix/flaky", 0.94, 500),
    (5 * 24 * H, "projects/infra", "Terraform state cleanup", True,
     "Remove the orphaned S3 buckets from state", "Plan looks good, apply it",
     "Applied. 3 resources removed from state, nothing destroyed.", "cli", "main", 1.61, 800),
    (9 * 24 * H, "projects/blog", "Blog post: profiling Python", False,
     "Help me outline a post on profiling", "Tighten the intro",
     "Intro cut to three sentences.", "cli", "drafts", 0.52, 180),
    (20 * 24 * H, "projects/infra", "Kubernetes upgrade notes", False,
     "What changes in the 1.31 upgrade?", "Summarize the breaking ones",
     "Two breaking changes affect us: ...", "cli", "", 0.88, 400),
]

# Older sessions so the list fills the pane the way a real store does.
for i, (sub, title) in enumerate([
        ("projects/acme-api", "Pagination for list endpoints"), ("projects/acme-web", "Upgrade to React 20"),
        ("projects/infra", "Cut CI time in half"), ("projects/ml-notebook", "Clean up feature pipeline"),
        ("dotfiles", "Neovim LSP setup"), ("projects/blog", "RSS feed fixes"),
        ("projects/acme-api", "Postgres index audit"), ("projects/acme-web", "Accessibility pass"),
        ("projects/infra", "Rotate TLS certificates"), ("projects/acme-api", "OpenAPI spec cleanup")]):
    SESSIONS.append(((24 + 4 * i) * 24 * H, sub, title, i % 3 == 0, title, "Looks good, thanks",
                     "Done.", "cli", "main", 0.5 + i / 10, 100 + 90 * i))


def build_store():
    projects = fake_home / ".claude" / "projects"
    for age, sub, title, renamed, first, last, reply, kind, branch, cost, pad in SESSIONS:
        cwd = f"{fake_home}/{sub}"
        Path(cwd).mkdir(parents=True, exist_ok=True)
        d = projects / cwd.replace("/", "-")
        d.mkdir(parents=True, exist_ok=True)
        sid = str(uuid.uuid4())
        ep = "sdk-py" if kind == "sdk" else "cli"
        recs = [{"type": "user", "entrypoint": ep, "cwd": cwd, "gitBranch": branch,
                 "message": {"role": "user", "content": first}}]
        if kind == "team":
            recs.append({"type": "user", "teamName": "oauth-migration",
                         "agentName": "reviewer" if pad == 120 else "auditor", "cwd": cwd})
        recs.append({"type": "assistant", "cwd": cwd,
                     "message": {"role": "assistant", "model": "claude-opus-5-5",
                                 "content": [{"type": "text", "text": "Working on it."}]}})
        filler = "x" * 1000
        recs += [{"type": "progress", "data": filler}] * pad
        if last:
            recs.append({"type": "user", "cwd": cwd, "message": {"role": "user", "content": last}})
            recs.append({"type": "last-prompt", "lastPrompt": last})
        recs.append({"type": "assistant", "cwd": cwd,
                     "message": {"role": "assistant", "model": "claude-opus-5-5",
                                 "content": [{"type": "text", "text": reply}]}})
        if kind == "live":
            # Still running: no cost record yet, only per-reply usage, so the
            # picker shows an estimate priced from the other sessions' records.
            recs.append({"type": "assistant", "cwd": cwd, "timestamp": "2026-01-01T00:00:00Z",
                         "message": {"id": "msg_live", "role": "assistant", "model": "claude-opus-5-5",
                                     "usage": {"input_tokens": 9000, "output_tokens": 61000,
                                               "cache_read_input_tokens": 2400000,
                                               "cache_creation_input_tokens": 180000},
                                     "content": [{"type": "text", "text": reply}]}})
        elif cost:
            recs.append({"type": "cost-state", "totalCostUSD": cost,
                         "totalLinesAdded": int(cost * 210), "totalLinesRemoved": int(cost * 60),
                         "modelUsage": {"claude-opus-5-5": {
                             "inputTokens": 1000, "outputTokens": int(cost * 8000), "cacheReadInputTokens": 0,
                             "cacheCreationInputTokens": 0, "costUSD": cost}}})
        if title:
            recs.append({"type": "ai-title", "aiTitle": title})
            if renamed:
                recs.append({"type": "custom-title", "customTitle": title})
        p = d / f"{sid}.jsonl"
        p.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in recs) + "\n")
        os.utime(p, (NOW - age, NOW - age))
        if kind == "lead":
            t = fake_home / ".claude" / "teams" / "oauth-migration"
            t.mkdir(parents=True, exist_ok=True)
            (t / "config.json").write_text(json.dumps(
                {"name": "oauth-migration", "leadSessionId": sid, "members": [1, 2, 3]}))
    # A session with no cwd on any record: the directory is decoded from the
    # store's folder name, which is lossy, so the picker marks it with "?".
    d = projects / f"{fake_home}/projects/legacy".replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{uuid.uuid4()}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in (
        {"type": "user", "entrypoint": "cli", "message": {"role": "user", "content": "Port the old cron jobs"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Ported 6 jobs to systemd timers."}]}},
        {"type": "ai-title", "aiTitle": "Port legacy cron jobs"})) + "\n")
    os.utime(p, (NOW - 12 * 24 * H, NOW - 12 * 24 * H))


def load_tool():
    src = HERE.parent / "claude-sessions"
    mod = types.ModuleType("cs")
    exec(compile(src.read_text(), str(src), "exec"), mod.__dict__)
    return mod


def scrub(out):
    svg = out.read_text()
    # The demo HOME lives in /tmp; show a normal-looking home instead.
    svg = svg.replace(str(fake_home), "/home/dev")
    # Crop away the fake window chrome (title bar, traffic lights, frame) by
    # pointing the viewBox at the terminal area alone.
    m = re.search(r'clip-terminal">\s*<rect x="0" y="0" width="([\d.]+)" height="([\d.]+)"', svg)
    t = re.search(r'<g transform="translate\(([\d.]+), ([\d.]+)\)" clip-path="url\(#[^)]*clip-terminal\)">', svg)
    if m and t:
        w, h = float(m[1]), float(m[2])
        svg = re.sub(r'viewBox="[^"]*"', f'viewBox="{t[1]} {t[2]} {w:.1f} {h:.1f}"', svg, count=1)
        svg = svg.replace('<svg class="rich-terminal"', f'<svg width="{w:.0f}" height="{h:.0f}" class="rich-terminal"', 1)
    out.write_text(svg)
    print("wrote", out)


async def shoot(cs, name, keys=(), query="", hover=None):
    rows, total = cs.collect(300, False, False, include_teams=True, include_scratch=True,
                             limit_counts_visible=True)
    app = cs.build_app(rows, query, False, total, False)
    async with app.run_test(size=(132, 62)) as pilot:
        await pilot.pause()
        for k in keys:
            await pilot.press(k)
            await pilot.pause()
        if hover:
            await pilot.hover("#list", offset=hover)
        await pilot.pause(0.3)
        out = HERE / f"{name}.svg"
        app.save_screenshot(filename=out.name, path=str(HERE))
        scrub(out)


def shoot_list(name):
    """`claude-sessions -l`, rendered the way a terminal shows it."""
    from rich.console import Console
    from rich.text import Text
    res = subprocess.run([sys.executable, str(HERE.parent / "claude-sessions"), "-l", "-t", "-s"],
                         capture_output=True, text=True)
    con = Console(record=True, width=170, file=open(os.devnull, "w"))
    con.print(Text("$ claude-sessions -l -t", style="bold #7aa2f7"))
    con.print(Text(res.stdout.rstrip()))
    con.print(Text(res.stderr.rstrip(), style="#565f89"))
    out = HERE / f"{name}.svg"
    out.write_text(con.export_svg(title="claude-sessions -l"))
    scrub(out)


async def main():
    build_store()
    cs = load_tool()
    # Default view: second row selected, mouse hovering the fourth, preview on.
    await shoot(cs, "picker", keys=("down",), hover=(30, 4))
    # Agents revealed with CTRL-T, a teammate selected so its preview shows the team.
    await shoot(cs, "agents", keys=("ctrl+t", "down", "down", "down"))
    # Fuzzy search, agents shown so teammates match on their directory too.
    await shoot(cs, "search", keys=("ctrl+t",), query="acme-api")
    # Preview hidden with CTRL-/: the whole list, including the "?" row.
    await shoot(cs, "list-only", keys=("ctrl+slash",))
    shoot_list("plain-list")


if __name__ == "__main__":
    asyncio.run(main())

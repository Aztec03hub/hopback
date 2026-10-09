"""hopback's checks, against the fake store in demo_store.py.

Run either way:
    python tests/test_hopback.py
    python -m pytest tests
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TMP = Path(tempfile.mkdtemp(prefix="hopback-test-"))
HOME, WIN = TMP / "home", TMP / "winhome"
HOME.mkdir()
WIN.mkdir()
os.environ["HOME"] = str(HOME)
os.environ["HOPBACK_WINDOWS_HOME"] = str(WIN)
os.environ["XDG_STATE_HOME"] = str(TMP / "state")  # never the real hidden list
os.environ["XDG_CACHE_HOME"] = CACHE = str(TMP / "cache")   # nor the real launch cache

import demo_store  # noqa: E402
from hopback import cli, paths, readers  # noqa: E402
from hopback.adapters import AGENT, claude  # noqa: E402
from hopback.sources import discover  # noqa: E402

IDS = demo_store.build(HOME, WIN)


def load(**kw):
    opts = dict(include_teams=True, include_scratch=True, limit_counts_visible=True)
    opts.update(kw)
    rows, total, warnings = cli.load_rows(discover(), 300, False, False, **opts)
    assert not warnings, warnings
    return rows, total


def row(sid):
    return next(r for r in load()[0] if r["id"] == sid)


def test_discovers_every_store():
    tags = sorted((s.tag, s.root.name) for s in discover())
    host = paths.this_host()
    assert tags == sorted([(f"claude·{host}", ".claude"), ("claude·win", ".claude"),
                           (f"codex·{host}", ".codex"), ("codex·win", ".codex"),
                           (f"hermes·{host}", ".hermes"), (f"hermes·{host}", "writer")]), tags
    assert [s.tag for s in discover(harness="codex", host="win")] == ["codex·win"]


def test_rows_carry_roles_and_sources():
    rows, total = load()
    roles = {r["id"]: r["role"] for r in rows}
    assert roles[IDS["claude-lead"]] == "lead"
    assert roles[IDS["claude-team"]] == "team"
    assert roles[IDS["claude-sdk"]] == "sdk"
    assert roles[IDS["codex-cli-subagent"]] == "sub"
    assert roles[IDS["codex-exec-user"]] == "exec"
    assert roles[IDS["hermes-d-cron"]] == "cron"
    assert roles[IDS["hermes-d-subagent"]] == "sub"
    assert roles[IDS["hermes-d-telegram"]] == "chat"
    assert roles[IDS["hermes-w-cli"]] == ""
    assert total == len(rows) + 0, (total, len(rows))
    assert rows == sorted(rows, key=lambda r: r["mtime"], reverse=True)
    win = row(IDS["claude-win"])
    assert win["source"].tag == "claude·win" and win["cwd"] == "C:\\Users\\dev\\src\\installer"
    assert row(IDS["codex-win"])["cwd"] == "C:\\Users\\dev\\src\\desktop"
    assert row(IDS["claude-guessed"])["guessed"]


def test_hidden_categories_are_separate():
    rows, _ = load()
    plain = [r for r in rows if cli.keep(r, False, False)]
    assert not any(r["role"] in AGENT | {"cron"} for r in plain)
    with_cron = [r for r in rows if cli.keep(r, False, True)]
    assert {r["role"] for r in with_cron} - {r["role"] for r in plain} == {"cron"}


def test_background_job_leftovers():
    """A session handed to a background job leaves its old file behind with a
    `continued-in` record. It is a stale copy only if nothing replied after the
    hand-over; a run inside a job's scratch directory is a leftover too."""
    import json
    root = TMP / "jobstore" / ".claude"
    proj = root / "projects" / "-p"
    proj.mkdir(parents=True)

    def session(sid, recs, cwd="/home/u/p"):
        recs = [{"type": "user", "cwd": cwd, "message": {"role": "user", "content": "hi"}},
                {"type": "assistant", "cwd": cwd, "message": {"role": "assistant", "content": "ok"}}] + recs
        (proj / f"{sid}.jsonl").write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs))

    hand_over = {"type": "continued-in", "continuedInSessionId": "new"}
    note = {"type": "user", "message": {"role": "user", "content": "<task-notification>"}}
    reply = {"type": "assistant", "message": {"role": "assistant", "content": "back again"}}
    session("new", [])
    session("stale", [hand_over, note])            # only a notification after: stale
    session("diverged", [hand_over, note, reply])  # resumed later: both are real
    session("dangling", [{"type": "continued-in", "continuedInSessionId": "gone"}])
    session("scratch", [], cwd="/home/u/.claude/jobs/06f5ccaf/tmp/tuitest")
    rows, _, problems = claude.collect(root, None, False, False, include_empty=True)
    assert problems == [], problems
    roles = {r["id"]: r["role"] for r in rows}
    assert roles == {"new": "", "stale": "copy", "diverged": "", "dangling": "", "scratch": "job"}, roles
    shown = {r["id"] for r in rows if cli.keep(r, False, False)}
    assert shown == {"new", "diverged", "dangling"}, shown
    assert {r["id"] for r in rows if cli.keep(r, False, False, True)} == set(roles)
    fields = dict(claude.details(root, "stale")["fields"])
    assert fields["continued in"].startswith("new ")
    assert "continued in" not in dict(claude.details(root, "dangling")["fields"])
    assert not claude.is_job_scratch("/home/u/.claude/jobs/06f5ccaf")


def test_previews_read_real_messages():
    p = cli.preview(row(IDS["codex-cli-user"]), 100)
    assert "Also cover the empty-page case" in p, p
    assert "<environment_context>" not in p  # injected context is not a prompt
    assert "last msg from Codex" in p and "Covered: an empty page" in p
    assert "1.8M in (1.6M cached) · 42K out" in p
    newfmt = cli.preview(row(IDS["codex-win"]), 100)  # item_completed format
    assert "Now bump the version to 2.4" in newfmt and "Bumped to 2.4.0" in newfmt, newfmt
    h = cli.preview(row(IDS["hermes-w-cli"]), 100)
    assert "profile          writer" in h and "Make the ending ambiguous" in h, h
    sub = cli.preview(row(IDS["hermes-d-subagent"]), 100)
    assert "spawned by       Draft the Q3 roadmap" in sub, sub
    c = cli.preview(row(IDS["claude-lead"]), 100)
    assert "LEAD of oauth-migration" in c and "lines changed    +1,869 / -534" in c, c
    live = cli.preview(row(IDS["claude-live"]), 100)
    assert "cost             ≈ $" in live, live


def test_resume_commands():
    def argv(sid, yolo=False):
        r = row(sid)
        return r["source"].adapter.resume_cmd(r["source"].root, r, yolo)
    assert argv(IDS["claude-cli"], True) == ["claude", "--resume", IDS["claude-cli"],
                                             "--dangerously-skip-permissions"]
    assert argv(IDS["codex-cli-user"], True)[-1] == "--dangerously-bypass-approvals-and-sandbox"
    assert argv(IDS["hermes-d-cli"]) == ["hermes", "-p", "default", "--resume", IDS["hermes-d-cli"]]
    assert argv(IDS["hermes-w-cli"], True) == ["hermes", "-p", "writer", "--resume",
                                               IDS["hermes-w-cli"], "--yolo"]


def test_windows_sessions_launch_on_windows():
    r = row(IDS["claude-win"])
    if paths.this_host() != "wsl" or not paths.powershell():
        return  # only meaningful inside WSL with Windows reachable
    run, where, shown = r["source"].launch(r, False)
    assert run[0].endswith(".exe") and "-Command" in run and where.startswith("/mnt/")
    script = run[-1]
    assert "Set-Location -LiteralPath 'C:\\Users\\dev\\src\\installer'" in script, script
    assert f"& 'claude' '--resume' '{IDS['claude-win']}'" in script, script
    assert "is not installed on Windows" in script
    assert "-ErrorAction Stop" in script
    bad = dict(r, id="x'; Remove-Item C:\\ -Recurse; '")
    try:
        r["source"].launch(bad, False)
        raise AssertionError("an injected id was accepted")
    except RuntimeError:
        pass
    assert shown.startswith("[Windows] cd C:\\Users\\dev\\src\\installer && claude --resume")


def test_paths():
    assert paths.clean_win("\\\\?\\C:\\a\\b") == "C:\\a\\b"
    assert paths.to_windows("/mnt/c/Users/x") == "C:\\Users\\x"
    assert paths.to_windows("/home/x") == "/home/x"
    assert paths.ps_quote("it's") == "'it''s'"
    # Typographic quotes end a PowerShell string too, so they must be doubled.
    assert paths.ps_quote("Bob\u2019s; evil") == "'Bob\u2019\u2019s; evil'"
    if paths.this_host() == "wsl":
        assert paths.to_local("C:\\Users\\x\\a b") == "/mnt/c/Users/x/a b"
        assert paths.to_local("\\\\?\\D:\\p") == "/mnt/d/p"
    assert claude.decode_dir(Path("C--Users-dev-src")) == "C:\\Users\\dev\\src"
    assert claude.decode_dir(Path("-home-dev-x")) == "/home/dev/x"


def test_lines_backwards_across_chunk_boundaries():
    f = TMP / "lines.txt"
    lines = [f"line {i} " + "x" * (i % 7) for i in range(500)]
    f.write_text("\n".join(lines) + "\n")
    for chunk in (1, 3, 17, 64, 10_000):
        got = [b.decode() for b in readers.lines_backwards(f, chunk=chunk)]
        assert got == lines[::-1], chunk


def test_exact_matches_rank_first():
    rows, _ = load()
    got = cli.rank([r for r in rows if r["source"].host == "win" or "acme" in r["cwd"]], "acme-api")
    first_fuzzy = next(i for i, r in enumerate(got) if "acme-api" not in cli.haystack(r).lower())
    assert all("acme-api" in cli.haystack(r).lower() for r in got[:first_fuzzy])
    assert all("acme-api" not in cli.haystack(r).lower() for r in got[first_fuzzy:])


def test_one_exchange_preview_says_same_not_missing():
    p = cli.preview(row(IDS["claude-team"]), 100)
    assert "(the same as the last prompt)" in p, p


def test_fuzzy_words_are_anded():
    assert cli.fuzzy("acme api", "Rate limiter ~/projects/acme-api")
    assert cli.fuzzy("api acme", "~/projects/acme-api")
    assert not cli.fuzzy("acme zebra", "~/projects/acme-api")
    assert cli.fuzzy("codex win", f"x {row(IDS['codex-win'])['source'].tag} OpenAI Codex on Windows")


def test_picker_controls():
    rows, total = load()

    async def drive():
        app = cli.build_app(rows, "", False, total, False, discover())
        async with app.run_test(size=(150, 50)) as p:
            async def press(*keys):
                # Filters refresh in a worker; wait for it, or the asserts race it.
                await p.press(*keys)
                await app.workers.wait_for_complete()
                await p.pause()

            await p.pause()
            base = len(app._visible)
            await press("tab")
            assert app.harness == "claude"
            await press("tab", "tab", "tab")
            assert app.harness is None and len(app._visible) == base
            await press("ctrl+o")
            assert app.host == paths.this_host()
            await press("ctrl+o")
            assert app.host == "win" and {r["source"].host for r in app._visible} == {"win"}
            await press("ctrl+o")
            await press("ctrl+r")
            assert len(app._visible) == base + 2, (len(app._visible), base)  # two cron runs
            await press("ctrl+t")
            assert any(r["role"] == "team" for r in app._visible)
            await p.click("#sched")
            await app.workers.wait_for_complete()
            assert not app.show_scheduled
            from textual.widgets import Input
            # A fuzzy query over random temp paths can match anything; the id is exact.
            app.query_one("#search", Input).value = IDS["hermes-d-cli"]
            await p.pause()
            assert [r["id"] for r in app._visible] == [IDS["hermes-d-cli"]], app._visible
    asyncio.run(drive())


def test_model_rates_scans_a_store_once():
    """From the desktop's perf-preview-cache branch: the rate scan is kept per store."""
    store = HOME / ".claude" / "projects"
    claude._RATES.clear()
    calls = []
    real = claude.last_line_with
    claude.last_line_with = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    try:
        claude.model_rates({"no-such-model"}, store)
        first = len(calls)
        claude.model_rates({"no-such-model"}, store)
        claude.model_rates({"another-missing-model"}, store)
    finally:
        claude.last_line_with = real
    assert first and len(calls) == first, (first, len(calls))


def test_shell_launch_parser():
    """The parser's own table of launches and look-alikes (hopback/shellparse.py)."""
    from hopback import shellparse
    shellparse.self_check()


def test_spawned_sessions():
    """Only a session whose opening prompt came out of another Claude's own
    tool call, made just before it started, counts as spawned."""
    import json
    from datetime import datetime, timezone
    from hopback import launches
    root = TMP / "botstore" / ".claude"
    proj = root / "projects" / "-p"
    proj.mkdir(parents=True)
    T = 1_800_000_000

    def ts(t):
        return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def write(sid, recs):
        (proj / f"{sid}.jsonl").write_text(
            "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs))

    def call(t, tool="Bash", **inp):
        return {"type": "assistant", "timestamp": ts(t), "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "x", "name": tool, "input": inp}]}}

    def said(t, role, text):
        return {"type": role, "timestamp": ts(t), "message": {"role": role, "content": text}}

    inline = "You are lane-x, an implementer for the lead. Read the brief and build it."
    typed = "You are a reviewer of the cache layer. Read the plan, then report back please."
    pasted = "Summarise the quarterly numbers for me and draft the email to the whole team."
    late = "You are a late starter that began well after the launch command finished."
    write("parent", [
        said(T - 120, "user", pasted),                      # a person's own message
        call(T - 100, command=f"tmux new-window -d \"claude --model sonnet 'You are \\\"lane-x\\\", an implementer for the lead. Read the brief and build it.'\""),
        call(T - 50, command="tmux new-window -d -n rev 'claude --dangerously-skip-permissions'"),
        call(T - 40, command=f"tmux send-keys -t rev -l '{typed}' && tmux send-keys -t rev Enter"),
        call(T - 300, command=f"claude --model sonnet '{late}'"),
    ])
    write("bot1", [said(T - 99, "user", inline)])               # quotes stripped by the shell
    write("bot2", [{"type": "mode", "timestamp": ts(T - 49)}, said(T - 39, "user", typed)])
    write("human", [said(T - 95, "user", pasted)])              # pasted, never in a tool call
    write("slow", [said(T - 300 + 60, "user", late)])           # 60 s after its launch
    # Pasted text inside a tool call but with no launch: still not spawned.
    write("other", [call(T - 2000, command=f"echo '{late}'")])

    # False-positive traps from review: `claude` that isn't a launch, and text
    # the parent only read, not sent.
    handoff = "Continue the migration work from the handoff note and keep the tests green."
    write("lead2", [
        call(T + 1000, "Write", file_path="/w/docs/HANDOFF.md", content=handoff),
        call(T + 1090, command="git commit -m 'docs: claude handoff'"),     # not a launch
        call(T + 2000, "Read", file_path="/w/docs/HANDOFF-claude-session-notes.md"),
        call(T + 2005, command="pkill claude; rg claude /w"),              # not a launch
    ])
    write("paste1", [said(T + 1102, "user", handoff)])
    write("paste2", [said(T + 2010, "user", "/w/docs/HANDOFF-claude-session-notes.md and go on from it")])
    # A prompt file written with Write, then passed in by name: spawned.
    brief = "You are the E1 launcher for an agent experiment. Make exactly one Agent call."
    write("lead3", [
        call(T + 3000, "Write", file_path="/w/exp/E1-launcher-prompt.txt", content=brief),
        call(T + 3060, command='tmux new-window -d "claude --dangerously-skip-permissions \\"$(cat $X/E1-launcher-prompt.txt)\\""'),
    ])
    write("e1", [said(T + 3061, "user", brief)])
    # Two copies of one conversation hold the same launch: the newer is named.
    copied = "You are the copy test implementer. Build the unit and report to the lead."
    launch = call(T + 4000, command=f"claude '{copied}'")
    write("orig", [launch])
    write("cont", [launch, said(T + 4100, "user", "later work")])
    os.utime(proj / "orig.jsonl", (1_000_000_000, 1_000_000_000))   # the older copy
    write("kid", [said(T + 4001, "user", copied)])

    # One guard at a time. `claude` not in command position, though the
    # command does carry the text a person then pastes:
    noted = "Fix the flaky date test in the billing module and add a regression test."
    write("lead4", [call(T + 5000, command=f"pkill claude; echo '{noted}' >> notes.txt")])
    write("paste3", [said(T + 5005, "user", noted)])
    # A real launch, plus a file it never names that a person then pastes from:
    stray = "Draft the release notes for version two and list every breaking change."
    write("lead5", [
        call(T + 6000, "Write", file_path="/w/notes/release.md", content=stray),
        call(T + 6010, command="claude --model sonnet 'Run the nightly smoke tests and report back.'"),
    ])
    write("paste4", [said(T + 6015, "user", stray)])
    # A launch that does name a prompt file, and a path the parent only Read:
    path = "/w/projects/acme-platform/docs/briefs/overview-of-the-billing-system.md"
    write("lead6", [
        call(T + 7000, "Read", file_path=path),
        call(T + 7010, command='claude "$(cat /w/briefs/nightly-prompt.txt)"'),
    ])
    write("paste5", [said(T + 7015, "user", path)])

    # A handoff written with a heredoc that mentions `claude`, then pasted:
    note = "Pick up the parser refactor from the notes below and finish the last two steps."
    write("lead7", [call(T + 8000, command=f"cat > H.md <<'EOF'\n{note}\nRun `claude` to start.\nclaude --model x\nEOF")])
    write("paste6", [said(T + 8010, "user", note)])

    problems = []
    got = launches.started_by(root, problems)
    assert got == {"bot1": "parent", "bot2": "parent", "e1": "lead3", "kid": "cont"}, got
    assert launches.started_by(root, problems) == got and problems == []   # warm cache, same answer
    rows, _, _ = claude.collect(root, None, False, False, include_empty=True)
    roles = {r["id"]: r["role"] for r in rows}
    assert roles["bot1"] == roles["bot2"] == "spawn" and roles["human"] == "", roles
    # `hopback -l -n N` shows N sessions: hidden spawn rows don't spend the limit.
    shown = [r for r in rows if cli.keep(r, False, False, False)]
    limited, _, _ = claude.collect(root, len(shown), False, False, include_teams=False, include_empty=True)
    assert len([r for r in limited if cli.keep(r, False, False, False)]) == len(shown), limited
    fields = dict(claude.details(root, "bot1")["fields"])
    assert fields["started by"].endswith("(parent)"), fields
    # An unreadable parent is reported and left undecided, never settled as "no".
    os.environ["XDG_CACHE_HOME"] = str(TMP / "botcache-unreadable")
    parent = next(root.glob("projects/*/parent.jsonl"))
    if os.getuid() != 0:                       # root reads anything
        # Cold: the parent can't be read at all, and the children are long
        # settled. They must stay undecided, not become "not spawned" for good.
        parent.chmod(0)
        real_settle, launches.SETTLE = launches.SETTLE, -1e12
        try:
            problems = []
            got2 = launches.started_by(root, problems)
            assert "bot1" not in got2 and any("not checked" in p for p in problems), (got2, problems)
            assert "bot1" not in launches.load_cache(launches.cache_path(root))["verdicts"]
        finally:
            parent.chmod(0o644)
            launches.SETTLE = real_settle
        # Warm: its launches are cached, and only the verdict step reads it.
        launches.started_by(root, [])
        cpath = launches.cache_path(root)
        cache = json.loads(cpath.read_text())
        del cache["verdicts"]["bot1"]
        cpath.write_text(json.dumps(cache))
        parent.chmod(0)
        try:
            problems = []
            got3 = launches.started_by(root, problems)
            assert "bot1" not in got3 and any("not checked" in p for p in problems), (got3, problems)
            assert "bot1" not in json.loads(cpath.read_text())["verdicts"]   # undecided, not "no"
        finally:
            parent.chmod(0o644)
    assert launches.started_by(root, [])["bot1"] == "parent"   # decided once readable
    os.environ["XDG_CACHE_HOME"] = CACHE


def test_foreign_owned_tmp():
    """Paths Claude Code would refuse to start over are found; a symlink to a
    root-owned file is not one, and a missing directory is fine."""
    base = TMP / "claude-tmp"
    (base / "a" / "b").mkdir(parents=True)
    (base / "a" / "b" / "deep.txt").write_text("x")
    (base / "link").symlink_to("/etc/passwd")          # root-owned target
    locked = base / "locked"
    locked.mkdir()
    locked.chmod(0)                                   # ours, just unreadable
    try:
        assert cli.foreign_owned(base) == []
    finally:
        locked.chmod(0o700)
    assert cli.foreign_owned(TMP / "missing") == []
    real_lstat, deep = os.lstat, str(base / "a" / "b" / "deep.txt")

    class Foreign:
        st_uid = os.getuid() + 1

    def fake_lstat(path, *a, **kw):
        return Foreign() if str(path) == deep else real_lstat(path, *a, **kw)
    os.lstat = fake_lstat
    try:
        assert cli.foreign_owned(base) == [deep]
    finally:
        os.lstat = real_lstat
    real_getuid = os.getuid
    os.getuid = lambda: real_getuid() + 1
    try:
        assert cli.foreign_owned(base) == [str(base)]     # root itself is foreign
    finally:
        os.getuid = real_getuid


def test_review_of_spawned_sessions():
    """The Review view's data: every marked session with its evidence, the
    ones that change the list first; verdicts persist; a rejection puts the
    session back in the user's list."""
    import json
    from datetime import datetime, timezone
    from hopback.sources import Source
    root = TMP / "reviewstore" / ".claude"
    proj = root / "projects" / "-w"
    proj.mkdir(parents=True)
    T = 1_800_000_000

    def ts(t):
        return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")

    def write(sid, recs):
        (proj / f"{sid}.jsonl").write_text(
            "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs))   # as Claude writes
    prompt = "You are the review test implementer. Build the unit and report back to the lead."
    write("lead", [{"type": "custom-title", "customTitle": "The lead"},
                   {"type": "assistant", "timestamp": ts(T), "cwd": "/w", "message": {
                       "role": "assistant", "content": [{"type": "tool_use", "name": "Bash",
                                                         "input": {"command": f"claude '{prompt}'"}}]}}])
    write("kid", [{"type": "user", "timestamp": ts(T + 1), "cwd": "/w",
                   "message": {"role": "user", "content": prompt}},
                  {"type": "assistant", "timestamp": ts(T + 2), "cwd": "/w",
                   "message": {"role": "assistant", "content": "ok"}}])
    os.environ["XDG_CACHE_HOME"] = str(TMP / "reviewcache")
    try:
        src = Source(paths.this_host(), claude, root)
        rows, problems = cli.review_rows([src])
        assert [r["id"] for r in rows] == ["kid"] and rows[0]["review_changes"] and not problems, rows
        text = cli.preview(rows[0])
        assert "why it was marked" in text and "The lead" in text and "1.0 s after" in text, text
        cli.save_review({"kid": "rejected"})
        assert cli.load_review() == {"kid": "rejected"}
        listed, _, _ = claude.collect(root, None, False, False, include_empty=True)
        kid = next(r for r in listed if r["id"] == "kid")
        assert kid["role"] == "spawn"
        cli.apply_review(listed, cli.load_review())
        assert kid["role"] == ""                      # rejected: shown as the user's again
        cli.apply_review(listed, {"kid": "accepted"})
        assert kid["role"] == "spawn" and kid["team"].startswith("started by")   # undone
        cli.apply_review(listed, {})
        assert kid["role"] == "spawn"
        cli.review_path().write_text("[1, 2]")        # damaged file: no verdicts, no crash
        assert cli.load_review() == {}
        cli.review_path().write_text('{"kid": ["x"]}')  # unhashable verdict: ignored
        assert cli.load_review() == {}
        # A detector bug is shown, never hidden, and the list still loads.
        from hopback import launches

        def broken(*_):
            raise KeyError("boom")
        real_started, real_known = launches.started_by, launches.known_parents
        launches.started_by = launches.known_parents = broken
        try:
            got, _, warns = cli.load_rows([src], None, False, False, include_empty=True)
            assert {r["id"] for r in got} >= {"lead", "kid"}, got
            assert any("spawn detection failed" in w and "boom" in w for w in warns), warns
            assert cli.review_rows([src]) == ([], [f"{src.tag}: spawn detection failed (KeyError: 'boom')"])
            assert "started by" in cli.preview(next(r for r in got if r["id"] == "kid")) \
                and "unknown: spawn detection failed" in cli.preview(next(r for r in got if r["id"] == "kid"))
        finally:
            launches.started_by, launches.known_parents = real_started, real_known
    finally:
        cli.save_review({})
        os.environ["XDG_CACHE_HOME"] = CACHE


def test_review_view_while_loading():
    """F12 then a mark before the scan returns records nothing and shows an
    empty list, not the main list with marks landing on its rows."""
    import time as _time
    rows, total = load()
    real = cli.review_rows
    cli.review_rows = lambda sources: (_time.sleep(1.0), ([], []))[1]
    try:
        async def drive():
            app = cli.build_app(rows, "", False, total, False, discover())
            async with app.run_test(size=(160, 50)) as p:
                await p.pause()
                await p.press("f12")
                await p.pause(0.2)
                assert app.view_review and app.review_rows is None
                assert app._visible == [], len(app._visible)
                await p.press("1")
                await p.pause(0.2)
                assert cli.load_review() == {}
                await app.workers.wait_for_complete()
                await p.pause()
                assert app.review_rows == [] and app.view_review
        asyncio.run(drive())
    finally:
        cli.review_rows = real
        cli.save_review({})


def test_review_marking():
    """Fast marks all land, each on its own row; the view opens on the first
    unmarked row; a mark a failed save couldn't write is saved by the next."""
    rows, total = load()
    fake = [dict(r, review=True, review_changes=True) for r in rows[:5]]
    # The [x] mark fits inside the NAME column: long names don't push the rest right.
    import time as _time
    long_, short = (dict(fake[0], name=n, review_mark="✓") for n in ("x" * 60, "short"))
    lines = [cli.fmt(r, 34, "/nowhere", _time.time()) for r in (long_, short)]
    assert len(lines[0]) == len(lines[1]), lines
    ids = [r["id"] for r in fake]
    real_rows, real_save = cli.review_rows, cli.save_review
    cli.review_rows = lambda sources: (fake, [])
    cli.save_review({ids[0]: "accepted"})
    try:
        async def drive():
            app = cli.build_app(rows, "", False, total, False, discover())
            async with app.run_test(size=(160, 50)) as p:
                await p.pause()
                # Outside the Review view, marking does nothing.
                app.action_mark("accepted")
                assert cli.load_review() == {ids[0]: "accepted"}
                app.query_one("#search").focus()
                await p.press("f12")
                await app.workers.wait_for_complete()
                await p.pause()
                lv = app.query_one("#list")
                assert lv.index == 1, lv.index             # the first unmarked
                # Once a search has begun, digits type instead of marking.
                await p.press("a", "1")
                assert app.query_one("#search").value == "a1" and cli.load_review() == {ids[0]: "accepted"}
                await p.press("ctrl+u")
                await p.pause()
                lv.index = 1
                await p.pause()
                # With something other than the search box focused (the tab
                # bar), digits still mark: they bubble up to the app.
                app.query_one("#tabs").focus()
                await p.pause()
                assert app.focused is not app.query_one("#search"), app.focused
                await p.press("2")
                assert cli.load_review().get(ids[1]) == "rejected", cli.load_review()
                cli.save_review({ids[0]: "accepted"})
                app.review = cli.load_review()
                app.query_one("#search").focus()
                lv.index = 1
                await p.pause()
                await p.press("2", "3", "1")               # no pauses between keys
                assert cli.load_review() == {ids[0]: "accepted", ids[1]: "rejected",
                                             ids[2]: "discuss", ids[3]: "accepted"}
                assert lv.index == 4 and "[✗]" in lv.lines[1]

                def fail(_):
                    raise OSError("disk full")
                cli.save_review = fail
                await p.press("2")                         # ids[4]: kept in memory only
                cli.save_review = real_save
                assert ids[4] not in cli.load_review()
                lv.index = 3
                await p.press("0")                         # clears ids[3], saves ids[4] too
                saved = {ids[0]: "accepted", ids[1]: "rejected", ids[2]: "discuss", ids[4]: "rejected"}
                assert cli.load_review() == saved
                assert "(1)" in str(app.query_one("#m-acc").render())   # ids[0] only
                assert "Review 4/5" in str(app.query_one("#reviewtab").render())
                lv.index = 4                               # the last row: the cursor stays
                await p.press("3")
                assert lv.index == 4 and cli.load_review()[ids[4]] == "discuss"
                saved[ids[4]] = "discuss"
                # A rebuild requested but not yet on screen: a mark must not
                # land on a row of the list that is about to be replaced.
                pending = app.refresh_rows()
                app.action_mark("accepted")
                assert cli.load_review() == saved
                await pending
                await p.pause()
                lv.index = 3
                app.action_mark("accepted")                # on screen now: it lands
                assert cli.load_review()[ids[3]] == "accepted"
                await p.pause()                            # let the cursor move settle before exit
        asyncio.run(drive())
    finally:
        cli.review_rows, cli.save_review = real_rows, real_save
        cli.save_review({})


def test_damaged_state_is_kept():
    """A damaged hidden/review file is moved aside and reported, never
    overwritten; an unreadable one raises instead of reading as empty."""
    for path, load in ((cli.hidden_path(), cli.load_hidden), (cli.review_path(), cli.load_review)):
        path.parent.mkdir(parents=True, exist_ok=True)
        for bad in (b"[", b'["a\xff"]', b"[" * 100000):     # truncated, bad UTF-8, too deep
            path.write_bytes(bad)
            cli.STATE_PROBLEMS.clear()
            assert not load() and not path.exists()
            kept = [p for p in path.parent.iterdir() if p.name.startswith(path.name + ".damaged-")]
            assert len(kept) == 1 and kept[0].read_bytes() == bad, kept
            assert cli.STATE_PROBLEMS and kept[0].name in cli.STATE_PROBLEMS[0], cli.STATE_PROBLEMS
            kept[0].unlink()
        cli.STATE_PROBLEMS.clear()
        # Two damaged copies in a row are both kept.
        for bad in (b"x", b"y"):
            path.write_bytes(bad)
            load()
        kept = sorted(p.read_bytes() for p in path.parent.iterdir() if p.name.startswith(path.name + ".damaged-"))
        assert kept == [b"x", b"y"], kept
        for p in list(path.parent.iterdir()):
            if p.name.startswith(path.name + ".damaged-"):
                p.unlink()
        cli.STATE_PROBLEMS.clear()
        # Another hopback saves a good file between this one's read and its
        # move: the good file must survive, not be set aside as damaged.
        good = b'{"x": "accepted"}' if load is cli.load_review else b'["claude:x"]'
        real_read = type(path).read_bytes
        state = {"raced": False}

        def racing_read(self, _real=real_read, _path=path):
            data = _real(self)
            if self == _path and not state["raced"]:
                state["raced"] = True          # damaged bytes read; the other instance saves now
                self.write_bytes(good)
                return b"["
            return data
        path.write_bytes(b"[")
        type(path).read_bytes = racing_read
        try:
            got = load()
        finally:
            type(path).read_bytes = real_read
        assert got and path.read_bytes() == good and not cli.STATE_PROBLEMS, (got, cli.STATE_PROBLEMS)
        assert not [p for p in path.parent.iterdir() if ".damaged-" in p.name]
        path.unlink()
        if os.getuid() != 0:                       # root reads anything
            path.write_text("{}" if load is cli.load_review else "[]")
            path.chmod(0)
            try:
                load()
                raise AssertionError("an unreadable file read as empty")
            except PermissionError:
                pass
            finally:
                path.chmod(0o644)
                path.unlink()


def test_plain_list_honours_hidden():
    """`hopback -l` runs end to end, leaves hidden sessions out, and --hidden
    lists only them."""
    import subprocess
    victim = row(IDS["claude-win"])
    cli.save_hidden({cli.hide_key(victim)})
    try:
        def ids(*flags):
            res = subprocess.run([sys.executable, "-m", "hopback", "-l", *flags], cwd=ROOT,
                                 capture_output=True, text=True, env=os.environ)
            assert res.returncode == 0, res.stderr
            return {ln.split()[-1] for ln in res.stdout.splitlines()[1:] if ln.strip()}
        assert victim["id"] not in ids()
        assert ids("--hidden") == {victim["id"]}
    finally:
        cli.save_hidden(set())


def test_hiding_sessions():
    """CTRL-X hides the selected session, the hidden view lists it with its
    harness, TAB never lands on that view, and the choice persists on disk."""
    rows, total = load()
    from textual.widgets import Tabs

    async def drive():
        app = cli.build_app(rows, "", False, total, False, discover())
        async with app.run_test(size=(160, 50)) as p:
            async def press(*keys):
                await p.press(*keys)
                await app.workers.wait_for_complete()
                await p.pause()

            await p.pause()
            base = len(app._visible)
            victim = app._visible[0]
            await press("ctrl+x")
            assert len(app._visible) == base - 1 and victim not in app._visible
            assert cli.hide_key(victim) in cli.load_hidden()
            label = str(app.query_one("#tabs", Tabs).query_one("#t-all").label)
            assert "(+1 hidden)" in label, label
            await p.click("#hiddentab")
            await app.workers.wait_for_complete()
            await p.pause()
            assert app.view_hidden and app._visible == [victim]
            assert app.query_one("#tabs", Tabs).active == ""
            # TAB leaves the hidden view for the harness tab you were on.
            await press("tab")
            assert not app.view_hidden and app.harness is None
            await press("tab", "tab", "tab")       # all -> claude -> codex -> hermes
            assert not app.view_hidden
            await press("ctrl+g")
            assert app.view_hidden
            await press("ctrl+x")                  # unhide from the hidden view
            assert app._visible == [] and not cli.load_hidden()
    asyncio.run(drive())


class Wheel:   # Pilot has no wheel; the handlers only need these
    def prevent_default(self): pass
    def stop(self): pass


def test_session_list_cursor_wheel_page_click():
    rows, total = load()

    async def drive():
        app = cli.build_app(rows, "", False, total, False, discover())
        async with app.run_test(size=(150, 30)) as p:   # short: rows overflow
            lv = app.query_one("#list")
            await p.pause()
            n = len(app._visible)
            assert n > lv.scrollable_content_region.height, (n, "test needs overflow")
            assert lv.index == 0 and len(lv.lines) == n
            for _ in range(n + 3):
                await p.press("down")
            await p.pause()
            assert lv.index == n - 1                     # clamped at the end
            top = int(lv.scroll_offset.y)
            assert top <= lv.index < top + lv.scrollable_content_region.height
            await p.press("pageup")
            assert lv.index == max(0, n - 11)
            lv.index = n - 1
            lv.on_mouse_scroll_up(Wheel())
            await p.pause()
            assert lv.index == n - 2                     # the wheel moves the selection
            lv.on_mouse_scroll_down(Wheel())
            assert lv.index == n - 1
            await p.press("ctrl+k", "ctrl+j", "ctrl+k")  # the same moves from the search box
            assert lv.index == n - 2
            await p.press("pagedown")
            assert lv.index == n - 1
            assert app.focused is app.query_one("#search")
            lv.index = 0
            await p.pause()
            assert int(lv.scroll_offset.y) == 0
            await p.click("#list", offset=(5, 2))        # a click selects AND resumes
            await p.pause()
        return app

    app = asyncio.run(drive())
    assert app.result and app.result[0] is app._visible[2], "click did not resume row 2"


def test_session_list_set_lines_set_line_and_hover():
    rows, total = load()

    async def drive():
        app = cli.build_app(rows, "", False, total, False, discover())
        async with app.run_test(size=(150, 40)) as p:
            await p.pause()
            lv = app.query_one("#list")
            lv.set_lines([], 3)
            assert lv.index is None                      # empty: no cursor
            lv.set_lines(["a", "b", "c"], 2)
            assert lv.index == 2
            lv.set_lines(["a", "b"], 5)
            assert lv.index == 1                         # clamped
            lv.set_line(0, "changed")
            assert lv.lines == ["changed", "b"] and lv.index == 1
            plain = lambda y: "".join(s.text for s in lv.render_line(y)).rstrip()
            assert plain(0) == " changed" and plain(5) == ""
            # Colours: cursor, stripe, hover.
            bg = lambda y: next(iter(lv.render_line(y))).style.bgcolor.name
            assert bg(1) == "#3b4261" and bg(0) == "#16161e"
            lv.set_lines(["a", "b", "c"], 0)
            assert bg(1) == "#262b3d"
            await p.hover("#list", offset=(4, 2))
            await p.pause()
            assert lv.hover == 2 and bg(2) == "#343b58"
            await p.hover("#list", offset=(4, 9))        # below the last row
            await p.pause()
            assert lv.hover is None
            lv.hover = 1
            lv.set_lines(["a"], 0)
            assert lv.hover is None                      # a rebuild clears it

    asyncio.run(drive())


def test_rebuild_refuses_actions_until_it_is_on_screen():
    rows, total = load()

    async def drive():
        app = cli.build_app(rows, "", False, total, False, discover())
        async with app.run_test(size=(150, 40)) as p:
            await p.pause()
            assert app._current() is not None
            pending = app.refresh_rows()                 # requested, not yet on screen
            assert app._shown_ticket != app._refresh_ticket
            assert app._current() is None                # refused
            app.action_resume()
            assert app.result is None                    # ... so ENTER did nothing
            await pending
            assert app._shown_ticket == app._refresh_ticket
            i, r = app._current()
            assert r is app._visible[i]
            # An overtaken rebuild returns without touching the list.
            old = app.refresh_rows()
            newest = app.refresh_rows()
            before = app.query_one("#list").lines
            await old
            assert app.query_one("#list").lines is before
            await newest
            assert app._current() is not None
            await p.pause()                              # let the cursor move settle before exit

    asyncio.run(drive())


def test_hidden_and_review_views_list_the_right_rows():
    rows, total = load()
    victim = rows[0]
    cli.save_hidden({cli.hide_key(victim)})
    fake = [dict(r, review=True, review_changes=True) for r in rows[:3]]
    real = cli.review_rows
    cli.review_rows = lambda sources: (fake, [])

    async def drive():
        app = cli.build_app(rows, "", False, total, False, discover())
        async with app.run_test(size=(160, 50)) as p:
            await p.pause()
            lv = app.query_one("#list")
            assert victim not in app._visible and len(lv.lines) == len(app._visible)
            await p.click("#hiddentab")
            await app.workers.wait_for_complete()
            await p.pause()
            assert app._visible == [victim] and len(lv.lines) == 1
            assert victim["name"][:20] in lv.lines[0] and lv.index == 0
            await p.press("f12")
            await app.workers.wait_for_complete()
            await p.pause()
            assert app.view_review and len(lv.lines) == 3 == len(app._visible)
            assert all(r["name"][:20] in ln for r, ln in zip(app._visible, lv.lines))
            # Marking edits the shown line in place and moves the cursor on.
            lv.index = 0
            app.action_mark("rejected")
            assert "[✗]" in lv.lines[0] and lv.index == 1
            await p.pause()
    try:
        asyncio.run(drive())
    finally:
        cli.review_rows = real
        cli.save_hidden(set())
        cli.save_review({})


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok    {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}: {exc.__class__.__name__}: {exc}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if failed else 0)

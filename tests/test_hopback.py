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
    rows, _ = claude.collect(root, None, False, False, include_empty=True)
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

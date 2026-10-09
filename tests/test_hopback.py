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


def test_preview_cache_round_trips_and_notices_changes():
    from hopback import previewcache
    r = dict(row(IDS["claude-lead"]))
    path = TMP / "cache" / "previews.json"
    pv = previewcache.Previewer(path=path)
    assert pv.get(r) is None
    d = pv.get_now(r)
    assert "LEAD of oauth-migration" in cli.render_preview(r, d, 100)
    pv.save()
    warm = previewcache.Previewer(path=path)
    assert warm.get(r) == d                  # a new run starts warm
    r["mtime"] += 1
    assert warm.get(r) is None               # a changed session is read again


def test_model_rates_scans_a_store_once():
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


def test_cursor_moves_never_read_sessions_on_the_ui_thread():
    import threading
    from hopback import previewcache
    rows, total = load()
    ui = []
    real = previewcache.build

    def spy(r):
        ui.append(threading.current_thread() is threading.main_thread())
        return real(r)

    async def drive():
        pv = previewcache.Previewer(persist=False)
        app = cli.build_app(rows, "", False, total, False, discover(), previewer=pv)
        async with app.run_test(size=(150, 50)) as p:
            for _ in range(8):
                await p.press("down")
            await p.press("up")
            for _ in range(100):           # let the background thread catch up
                await p.pause(0.02)
                if all(pv.get(r) for r in app._visible):
                    break
            assert all(pv.get(r) for r in app._visible), "prefetch did not finish"
            await p.pause(0.05)
            body = str(app.query_one("#preview_body").render())
            assert "reading…" not in body and "resume" in body, body

    previewcache.build = spy
    try:
        asyncio.run(drive())
    finally:
        previewcache.build = real
    assert ui and not any(ui), "details() ran on the UI thread"


def test_session_list_keeps_cursor_visible_and_wheel_moves_it():
    rows, total = load()

    async def drive():
        app = cli.build_app(rows, "", False, total, False, discover())
        async with app.run_test(size=(150, 30)) as p:   # short: rows overflow
            lv = app.query_one("#list")
            await p.pause()
            n = len(app._visible)
            assert n > lv.scrollable_content_region.height, "test needs overflow"
            for _ in range(n + 3):
                await p.press("down")
            await p.pause()
            assert lv.index == n - 1                     # clamped at the end
            top = int(lv.scroll_offset.y)
            assert top <= lv.index < top + lv.scrollable_content_region.height
            await p.press("pageup")
            assert lv.index == n - 11
            class Wheel:   # Pilot has no wheel; the handler only needs these
                def prevent_default(self): pass
                def stop(self): pass
            lv.on_mouse_scroll_up(Wheel())
            await p.pause()
            assert lv.index == n - 12                    # wheel moves the selection
            lv.index = 0
            await p.pause()
            assert int(lv.scroll_offset.y) == 0
            await p.click("#list", offset=(5, 2))
            await p.pause()
        return app

    app = asyncio.run(drive())
    assert app.result and app.result[0] is app._visible[2], "click did not resume row 2"


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

"""hopback's checks, against the fake store in demo_store.py.

Run either way:
    python tests/test_hopback.py
    python -m pytest tests
"""
import asyncio
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

_WORKER = __name__ == "__mp_main__"   # a spawn child re-imports this file: it must not leak a store
if _WORKER:
    TMP = Path(os.environ["XDG_STATE_HOME"]).parent   # the parent's store, inherited
    HOME, WIN = TMP / "home", TMP / "winhome"
    CACHE = os.environ["XDG_CACHE_HOME"]
else:
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

IDS = {} if _WORKER else demo_store.build(HOME, WIN)


async def until(p, cond, what, timeout=60):
    """Poll `cond` with the pilot's clock until a wall-clock deadline (a loaded
    machine can take many seconds to spawn the preview pool). Not
    app.workers.wait_for_complete(): that gathers the preview worker too, which
    the app cancels itself when a newer rebuild starts one, and the gather then
    raises WorkerCancelled."""
    deadline = time.monotonic() + timeout
    while True:
        await p.pause(0.01)
        if cond():
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} never finished")


async def rebuilt(app, p):
    await until(p, lambda: app._shown_ticket == app._refresh_ticket, "rebuild")


def setup_function(fn=None):
    """Before each test (pytest calls this; the runner below does too): an app
    that closed in an earlier test set STOP, which would cut a later scan short."""
    claude.STOP.clear()


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
                await rebuilt(app, p)
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
            await rebuilt(app, p)
            assert not app.show_scheduled
            from textual.widgets import Input
            # A fuzzy query over random temp paths can match anything; the id is exact.
            app.query_one("#search", Input).value = IDS["hermes-d-cli"]
            await p.pause()
            assert [r["id"] for r in app._visible] == [IDS["hermes-d-cli"]], app._visible
    asyncio.run(drive())


def _cost_session(proj, name, mtime, models, extra=None):
    """A session file whose last line is a cost-state record; `models` maps a
    model to its modelUsage entry. Returns the path."""
    import json
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{name}.jsonl"
    recs = [{"type": "user", "cwd": "/x", "message": {"role": "user", "content": "hi"}},
            {"type": "cost-state", "totalCostUSD": 1.0, "modelUsage": models}] + (extra or [])
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs))
    os.utime(path, (mtime, mtime))
    return path


def _usage(rate, tokens=100):
    """Output tokens weigh 5, so the weighted tokens are 5 * tokens: cost = rate * that."""
    return {"inputTokens": 0, "outputTokens": tokens, "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0, "costUSD": rate * 5 * tokens}


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
    # Warming reads the store; a later lookup then reads nothing.
    claude._RATES.clear()
    calls.clear()
    claude.last_line_with = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    try:
        claude.warm_rates(store)
        warmed = len(calls)
        claude.model_rates({"no-such-model"}, store)
    finally:
        claude.last_line_with = real
    assert warmed and len(calls) == warmed


def test_model_rates_values_and_selection_rules():
    store = TMP / "ratestore" / "projects"
    proj = store / "-p"
    T = 1_700_000_000
    older = _cost_session(proj, "older", T, {"m": _usage(2.0), "only-old": _usage(7.0)})
    newer = _cost_session(proj, "newer", T + 100, {"m": _usage(4.0), "zero": _usage(0.0)})
    # The weights are in/out/cache-read/cache-write = 1 / 5 / 0.1 / 1.25 (the aggregate fallback is for helper models, which write 5m cache).
    mixed = _cost_session(proj, "mixed", T - 100, {"w": {
        "inputTokens": 10, "outputTokens": 10, "cacheReadInputTokens": 100,
        "cacheCreationInputTokens": 8, "costUSD": 80.0 * 3}})
    claude._RATES.clear()
    near = lambda got, want: abs(got - want) < 1e-9
    r = claude.model_rates({"m", "only-old", "zero", "w", "absent"}, store)
    assert near(r["m"], 4.0), r                       # the newer file wins
    assert near(r["only-old"], 7.0) and near(r["w"], 3.0), r   # weighted tokens 10+50+10+10 = 80
    assert "zero" not in r and "absent" not in r, r   # costUSD 0 is no rate
    r = claude.model_rates({"m"}, store, skip=newer)
    assert near(r["m"], 2.0), r                       # skip: that file does not count
    claude._RATES.clear()
    r = claude.model_rates({"m", "only-old"}, store, max_files=1)
    assert near(r["m"], 4.0) and "only-old" not in r, r   # max_files=1: the newest file only
    claude._RATES.clear()


def test_model_rates_survive_malformed_records_and_vanished_files():
    store = TMP / "badratestore" / "projects"
    proj = store / "-p"
    T = 1_700_000_000
    _cost_session(proj, "good", T, {"opus": _usage(2.0)})
    _cost_session(proj, "bad", T + 100, {
        "other": {"inputTokens": None, "outputTokens": 3, "costUSD": 0.5},
        "text": {"inputTokens": "9", "outputTokens": "x", "costUSD": "1"},
        "list": [1, 2],
        "nan": {"inputTokens": float("nan"), "outputTokens": 1, "costUSD": 1.0},
        "inf": {"inputTokens": 1, "outputTokens": 1, "costUSD": float("inf")},   # _num needs isfinite
        "bool": {"inputTokens": True, "outputTokens": 3, "costUSD": 0.5},        # a bool is not a count
        "opus": _usage(5.0)})
    _cost_session(proj, "notdict", T + 200, [1, 2, 3])
    # A cost line json cannot parse for depth is no record, not an error.
    deep = proj / "deep.jsonl"
    deep.write_text('{"type":"cost-state","x":' + "[" * 100000 + "]" * 100000 + "}\n")
    os.utime(deep, (T + 300, T + 300))
    assert claude.last_line_with(deep, claude.COST_MARK) == (None, None)
    (proj / "gone.jsonl").symlink_to(proj / "no-such-target")   # globbed, then stat fails
    claude._RATES.clear()
    r = claude.model_rates({"opus", "other", "text", "list", "nan", "inf", "bool"}, store)
    assert r.keys() == {"opus"} and abs(r["opus"] - 5.0) < 1e-9, r   # a bad entry costs only itself
    # The worker initializer cannot raise on such data either.
    claude._RATES.clear()
    with mock.patch.object(cli, "_exit_with_parent"):   # the warm thread is under test, not the watcher
        cli._pool_init(os.getppid(), (str(store),)).join()
    assert store in claude._RATES
    claude._RATES.clear()


def _cost_store(base, n=1, cost=True):
    """A throwaway store with n session files, each with (or without) a cost-state line."""
    proj = base / "projects" / "p"
    proj.mkdir(parents=True)
    for i in range(n):
        line = ('{"type":"cost-state","modelUsage":{"opus":{"inputTokens":1000000,"costUSD":5.0}}}\n'
                if cost else '{"type":"user"}\n')
        (proj / f"s{i}.jsonl").write_text(line)
    return base / "projects"


def test_an_unreadable_or_unlistable_store_is_retried_not_cached():
    import io
    import contextlib
    store = _cost_store(TMP / "r3-unreadable")
    f = next(store.glob("*/*.jsonl"))
    f.chmod(0)
    try:
        claude._RATES.clear()
        if os.geteuid() != 0:
            assert not claude.model_rates({"opus"}, store)
            assert store in claude._RATES                  # permanent: kept, so not rescanned each preview
    finally:
        f.chmod(0o600)
    # A glob that fails with EIO: nothing on stderr from the warm thread, nothing cached.
    claude._RATES.clear()
    err = io.StringIO()
    with mock.patch.object(Path, "glob", side_effect=OSError(5, "EIO")), \
            mock.patch.object(cli, "_exit_with_parent"), contextlib.redirect_stderr(err), \
            mock.patch("threading.excepthook") as hook:
        cli._pool_init(os.getppid(), (str(store),)).join()
        assert not claude.model_rates({"opus"}, store)
    assert not hook.called and err.getvalue() == "" and store not in claude._RATES, (err.getvalue(), hook.call_args)
    claude._RATES.clear()


def test_rate_scan_caches_misses_and_runs_once_across_threads():
    import threading
    store = _cost_store(TMP / "r3-miss", n=3, cost=False)      # files, but no rate in any
    real = claude.last_line_with
    calls = []
    claude._RATES.clear()
    claude.last_line_with = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    try:
        for _ in range(3):
            assert claude.model_rates({"nobody"}, store) == {} or not claude.model_rates({"nobody"}, store)
        assert len(calls) == 3 and claude._RATES[store] == [], (len(calls), claude._RATES.get(store))
        # Two threads on one cold store: the scan runs once (the lock), not twice.
        claude._RATES.clear()
        calls.clear()
        entered, gate = threading.Event(), threading.Event()

        def slow(*a, **k):
            calls.append(1)
            entered.set()
            gate.wait(60)
            return real(*a, **k)
        claude.last_line_with = slow
        ts = [threading.Thread(target=claude.model_rates, args=({"nobody"}, store)) for _ in range(2)]
        ts[0].start()
        assert entered.wait(60)
        ts[1].start()
        gate.set()
        for t in ts:
            t.join(60)
        assert len(calls) == 3, len(calls)
    finally:
        claude.last_line_with = real
        claude._RATES.clear()


def test_pool_init_warms_every_store_and_only_claude_roots_are_handed_over():
    a, b = _cost_store(TMP / "r3-a"), _cost_store(TMP / "r3-b")
    claude._RATES.clear()
    with mock.patch.object(cli, "_exit_with_parent"):
        cli._pool_init(os.getppid(), (str(a), str(b))).join()
    assert a in claude._RATES and b in claude._RATES, list(claude._RATES)
    claude._RATES.clear()
    seen = []
    with mock.patch.object(cli, "preview_pool", side_effect=lambda stores=(): seen.append(list(stores))):
        async def drive():
            rows, total = load()
            app = cli.build_app(rows, "", False, total, False, discover())
            async with app.run_test(size=(120, 30)) as p:
                await p.pause()
        asyncio.run(drive())
    claude_roots = sorted({str(s.root / "projects") for s in discover() if s.adapter is claude})
    assert len(claude_roots) >= 2 and seen and seen[0] == claude_roots, (seen, claude_roots)


def test_a_cached_store_is_not_blocked_by_a_scan_of_another():
    """Two Claude stores (WSL and a slow /mnt/c): a lookup for one never waits
    for the other's scan, cached or not."""
    import threading
    a, b = TMP / "storeA" / "projects", TMP / "storeB" / "projects"
    T = 1_700_000_000
    _cost_session(a / "-p", "s", T, {"m": _usage(2.0)})
    _cost_session(b / "-p", "s", T, {"m": _usage(3.0)})
    claude._RATES.clear()
    claude.model_rates({"m"}, a)                       # A is cached
    real = claude.last_line_with
    entered, gate = threading.Event(), threading.Event()

    def slow(path, *x, **k):
        if b in path.parents:
            entered.set()
            gate.wait(60)
        return real(path, *x, **k)
    claude.last_line_with = slow
    got = {}
    scan_b = threading.Thread(target=lambda: got.__setitem__("b", claude.model_rates({"m"}, b)))
    try:
        scan_b.start()
        assert entered.wait(60), "B's scan never started"

        def look_a(key):
            got[key] = claude.model_rates({"m"}, a)
        for key in ("cached", "uncached"):
            if key == "uncached":
                claude._RATES.pop(a)                   # A must scan for itself, still not behind B
            t = threading.Thread(target=look_a, args=(key,))
            t.start()
            t.join(30)
            assert not t.is_alive(), f"a {key} lookup in store A waited for the scan of store B"
            assert abs(got[key]["m"] - 2.0) < 1e-9, got
        assert "b" not in got                          # B is still held
    finally:
        gate.set()
        scan_b.join(60)
        claude.last_line_with = real
    assert abs(got["b"]["m"] - 3.0) < 1e-9
    claude._RATES.clear()


def test_a_rate_scan_that_did_not_complete_is_not_kept():
    T = 1_700_000_000
    # A store with no file at all (an unmounted drive, an empty glob) is retried.
    empty = TMP / "emptystore" / "projects"
    empty.mkdir(parents=True, exist_ok=True)
    claude._RATES.clear()
    assert claude.model_rates({"m"}, empty) == {} and empty not in claude._RATES
    _cost_session(empty / "-p", "s", T, {"m": _usage(2.0)})
    assert abs(claude.model_rates({"m"}, empty)["m"] - 2.0) < 1e-9 and empty in claude._RATES
    # A scan the app's closing cut short is returned, not kept, and stops at the next file.
    store = TMP / "stopstore" / "projects"
    for i in range(3):
        _cost_session(store / "-p", f"s{i}", T + i, {f"m{i}": _usage(2.0)})
    real, seen = claude.last_line_with, []

    def stopper(path, *a, **k):
        seen.append(path)
        claude.STOP.set()
        return real(path, *a, **k)
    claude.last_line_with = stopper
    try:
        claude.model_rates({"m0", "m1", "m2"}, store)
    finally:
        claude.last_line_with = real
        claude.STOP.clear()
    assert len(seen) == 1 and store not in claude._RATES, (len(seen), store in claude._RATES)
    assert claude.model_rates({"m0", "m1", "m2"}, store).keys() == {"m0", "m1", "m2"}
    assert store in claude._RATES
    claude._RATES.clear()


def test_pool_init_warms_in_the_background_and_scans_once():
    """The initializer returns at once (a preview that needs no rate does not
    wait for the scan); a preview that needs one waits on the scan in
    progress instead of making a second. The scan is held on an event, so the
    test depends on no clock."""
    import threading
    store = HOME / ".claude" / "projects"
    nfiles = len(list(store.glob("*/*.jsonl")))
    claude._RATES.clear()
    real = claude.last_line_with
    calls, entered, gate = [], threading.Event(), threading.Event()

    def slow(*a, **k):
        calls.append(a[0])
        entered.set()
        gate.wait(60)
        return real(*a, **k)
    claude.last_line_with = slow
    try:
        with mock.patch.object(cli, "_exit_with_parent"):
            thread = cli._pool_init(os.getppid(), (str(store),))
        assert isinstance(thread, threading.Thread) and thread.daemon
        assert entered.wait(60), "the scan never started"
        # A preview with no cost (Codex) does not wait for it ...
        out = []
        t = threading.Thread(target=lambda: out.append(cli.preview(row(IDS["codex-cli-user"]), 100)))
        t.start()
        t.join(60)
        assert not t.is_alive() and out, "a non-cost preview waited for the scan"
        assert thread.is_alive()
        # ... one that needs a rate gets it from the single scan.
        gate.set()
        p = cli.preview(row(IDS["claude-live"]), 100)
        thread.join()
        assert "cost             ≈ $" in p, p
    finally:
        gate.set()
        claude.last_line_with = real
    # nfiles scanned once, plus the live session's own cost-record read.
    assert len(calls) == nfiles + 1, (len(calls), nfiles)
    claude._RATES.clear()
    # preview_pool hands the workers the stores to warm.
    pool = cli.preview_pool([store])
    if pool is not None:
        try:
            assert pool._initargs[1] == (str(store),), pool._initargs
        finally:
            pool.shutdown(wait=True, cancel_futures=True)


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
                await until(p, lambda: app.review_rows is not None, "review scan")
                await rebuilt(app, p)
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
                await rebuilt(app, p)
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


def test_review_view_opens_with_the_cursor_on_screen():
    """The Review view opens on an empty list ("scanning") and is then rebuilt
    with the first unmarked row, 40 rows down: the cursor must be inside the
    window although the list before it needed no scrollbar."""
    rows, total = load()
    fake = [dict(rows[0], id=f"rev{i:03d}", name=f"review row {i}", review=True, review_changes=True)
            for i in range(92)]
    import threading
    gate = threading.Event()
    real_rows = cli.review_rows
    cli.review_rows = lambda sources: (gate.wait(60), (fake, []))[1]
    cli.save_review({r["id"]: "accepted" for r in fake[:40]})
    try:
        async def drive():
            app = cli.build_app(rows, "", False, total, False, discover())
            async with app.run_test(size=(160, 30)) as p:
                await p.pause()
                lv = app.query_one("#list")
                await p.press("f12")
                await rebuilt(app, p)
                await p.pause(0.2)                     # the empty "scanning" list is laid out
                assert app.review_rows is None and not lv.lines
                gate.set()
                await until(p, lambda: app.review_rows is not None, "review scan")
                await rebuilt(app, p)
                await p.pause()
                top, h = int(lv.scroll_offset.y), lv.scrollable_content_region.height
                assert len(app._visible) == 92 and lv.index == 40, (len(app._visible), lv.index)
                assert top <= lv.index < top + h, (top, lv.index, h)
                await until(p, lambda: app._preview_key in app._previews, "preview")
        asyncio.run(drive())
        assert claude.STOP.is_set()                    # closing the app stops a rate scan
    finally:
        gate.set()
        cli.review_rows = real_rows
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
                await rebuilt(app, p)
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
            await rebuilt(app, p)
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
            # Page up at the top clamps to row 0 (and the preview follows).
            lv.index = 2
            await p.press("pageup")
            await p.pause()
            assert lv.index == 0 and app._current()[1] is app._visible[0]
            # Scrolled: a click resumes top + y, not row y.
            for _ in range(lv.scrollable_content_region.height + 2):
                await p.press("down")
            await p.pause()
            top = int(lv.scroll_offset.y)
            assert top > 0, top
            await p.click("#list", offset=(5, 2))        # a click selects AND resumes
            await p.pause()
        return app, top

    app, top = asyncio.run(drive())
    assert app.result and app.result[0] is app._visible[top + 2], "click did not resume row top+2"


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
            lv.set_lines(["a", "b", "c"], 2)
            lv.hover = 2
            assert bg(2) == "#3b4261"                    # the cursor row wins over hover
            lv.hover = 1
            assert bg(1) == "#343b58" and bg(2) == "#3b4261"
            assert bg(10) == "#16161e"                   # a blank row carries the row style
            lv.on_leave(None)
            assert lv.hover is None                      # the pointer left the list
            lv.hover = 1
            lv.set_lines(["a"], 0)
            assert lv.hover is None                      # a rebuild clears it
            # The cursor row is inside the window even when the list before it
            # fitted (no scrollbar yet): the scroll must not be skipped.
            lv.set_lines([], 0)
            lv.set_lines([f"r{i}" for i in range(100)], 50)
            top = int(lv.scroll_offset.y)
            assert top <= lv.index == 50 < top + lv.scrollable_content_region.height, (top, lv.index)
            # The wheel moves the selection one row at a time.
            lv.index = 3
            lv.on_mouse_scroll_down(Wheel())
            assert lv.index == 4
            lv.on_mouse_scroll_up(Wheel())
            assert lv.index == 3
            # A cursor that scrolls the list away from the pointer drops the hover, either way.
            lv.index = 0
            lv.hover = 2
            lv.index = 90
            assert lv.hover is None
            lv.hover = 85
            lv.index = 10
            assert lv.hover is None
            # The list never takes focus: a click selects (here without resuming)
            # and typing still goes to the search box.
            lv.set_lines(["a", "b", "c"], 0)
            with mock.patch.object(app, "action_resume") as resume:
                await p.click("#list", offset=(4, 2))
                await p.pause()
            assert resume.called and app.focused is app.query_one("#search"), app.focused
            await until(p, lambda: app._preview_key in app._previews, "preview")
            # Untrusted text: no control character reaches a screen line.
            lv.set_lines(["x\x1b[2Jy\nz\ttab\x07\x9b"], 0)
            lv.set_line(0, "q\x1b]0;title\x07r")
            assert not any(ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0 for ch in "".join(s.text for s in lv.render_line(0)))
            assert cli.safe_text("a\x1b[0mb\nc\td") == "a [0mb\nc\td" and cli.safe_text("a\nb\t", line=True) == "a b "
            await p.pause()

    asyncio.run(drive())


EVIL = ("EVIL\x1b]0;PWNED\x07\x1b[2J\x9b31m\N{RIGHT-TO-LEFT OVERRIDE} x\N{LEFT-TO-RIGHT ISOLATE}y"
        "\N{ZERO WIDTH SPACE}\N{WORD JOINER}\N{ZERO WIDTH NO-BREAK SPACE}\N{SOFT HYPHEN}"
        "\N{TAG LATIN CAPITAL LETTER A}\N{TAG LATIN SMALL LETTER B}\N{LEFT-TO-RIGHT MARK}")

# Format characters a real script needs, which safe_text keeps: the joiners, the
# Arabic, Syriac, Kaithi, Egyptian, shorthand and musical format marks.
_KEPT_FORMAT = ({0x200C, 0x200D, 0x06DD, 0x070F, 0x08E2, 0x110BD, 0x110CD}
                | set(range(0x600, 0x606)) | {0x890, 0x891} | set(range(0x13430, 0x13440))
                | set(range(0x1BCA0, 0x1BCA4)) | set(range(0x1D173, 0x1D17B)))


def _has_bad(text):
    """True when `text` holds a control, format, separator or surrogate code
    point, judged by the Unicode database and NOT by safe_text's own pattern
    (a character both forget would otherwise never be found). Newline, tab and
    the kept format characters are fine in a multi-line pane."""
    import unicodedata
    return any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp")
               and c not in "\n\t" and ord(c) not in _KEPT_FORMAT for c in text)


def test_safe_text_blanks_every_control_and_invisible_code_point():
    import unicodedata
    from hopback import fmt
    blanked = 0
    for cp in range(0x110000):
        c = chr(cp)
        if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Zl", "Zp") or cp in _KEPT_FORMAT:
            continue
        if c in "\n\t":
            assert fmt.safe_text(c) == c and fmt.safe_text(c, line=True) == " ", cp
        else:
            assert fmt.safe_text(c) == " " and fmt.safe_text(c, line=True) == " ", hex(cp)
            blanked += 1
    assert blanked > 150, blanked
    for cp in _KEPT_FORMAT | {0xFE0F, 0x1F600}:    # joiners, marks, a variation selector, an emoji
        assert fmt.safe_text(chr(cp)) == chr(cp), hex(cp)
    # A lone surrogate (valid JSON from a truncated emoji) would crash print().
    lone = "a" + chr(0xD83D) + "b"
    assert fmt.safe_text(lone) == "a b"
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        cli.plain([{**_hostile_row(), "name": lone}], 1, [])
    out.flush()


def _fake_source(**fns):
    """A Claude-shaped source whose adapter functions are overridden by `fns`."""
    import types
    from hopback.sources import Source
    fake = types.SimpleNamespace(NAME="claude", LABEL="Claude Code", ASSISTANT="Claude",
                                 resume_cmd=claude.resume_cmd, **fns)
    return Source("wsl", fake, HOME / ".claude" / "projects")


def _hostile_row():
    """A row whose every field carries EVIL, on an adapter whose details do too."""
    src = _fake_source(details=lambda root, sid: {
        "title": EVIL, "fields": [("cwd", EVIL)], "prompt": EVIL, "reply": EVIL,
        "opening": EVIL})
    r = dict(row(IDS["claude-live"]))
    r.update(source=src, name=EVIL, cwd="/tmp/" + EVIL, id="hostile")
    return r


class TTY(io.StringIO):
    def isatty(self):
        return True


def _run_main(*argv, chosen=None, sources=None, tty=True, foreign=()):
    """cli.main() with the given command line; the picker returns `chosen`, and
    neither the exec nor the chdir happens. Returns a namespace of the stdout
    and stderr text (a SystemExit message goes on stderr) and the mocks."""
    import types
    err, out = TTY(), TTY()
    with contextlib.ExitStack() as st:
        for m in (mock.patch.object(sys, "argv", ["hopback", *argv]),
                  mock.patch.object(sys, "stdin", TTY() if tty else io.StringIO()),
                  mock.patch.object(sys, "stdout", out if tty else io.StringIO()),
                  mock.patch.object(sys, "stderr", err),
                  mock.patch.object(cli, "pick", return_value=(chosen, False))):
            st.enter_context(m)
        st.enter_context(mock.patch.object(cli, "foreign_owned", return_value=list(foreign)))
        ex = st.enter_context(mock.patch.object(cli, "exec_agent"))
        chdir = st.enter_context(mock.patch.object(os, "chdir"))
        if sources is not None:
            st.enter_context(mock.patch.object(cli, "discover", return_value=sources))
        stdout = sys.stdout
        try:
            cli.main()
        except SystemExit as exc:
            err.write(str(exc.code or ""))
        text = out.getvalue() if tty else stdout.getvalue()
    return types.SimpleNamespace(out=text, err=err.getvalue(), exec=ex, chdir=chdir)


def test_untrusted_text_reaches_no_sink_raw():
    from hopback import fmt
    # The helper: the line variant also loses newline and tab; ZWJ stays (emoji).
    assert not _has_bad(fmt.safe_text(EVIL)) and not _has_bad(fmt.safe_text(EVIL, line=True))
    assert _has_bad(EVIL) and _has_bad("a\N{ZERO WIDTH SPACE}b") and _has_bad("a" + chr(0xD83D))
    assert fmt.safe_text("a\nb\t\N{ZERO WIDTH JOINER}c") == "a\nb\t\N{ZERO WIDTH JOINER}c"
    assert fmt.safe_text("a\nb\t", line=True) == "a b "
    bad = _hostile_row()
    # preview(): the final text, and its error string.
    assert not _has_bad(cli.preview(bad, 100))
    boom = _fake_source(details=mock.Mock(side_effect=OSError(EVIL)))
    assert not _has_bad(cli.preview({**bad, "source": boom}))
    # plain() (hopback -l).
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        cli.plain([bad], 1, [])
    assert "EVIL" in out.getvalue() and not _has_bad(out.getvalue().replace("\n", " "))
    # The launch command. The real resume gets the exact directory (a CRLF
    # script's directory ends in a CR, and was resumable before); only the
    # printed command renders control, invisible and bidi characters, as
    # $'\xNN' byte escapes that bash and zsh read back as the same bytes.
    src = row(IDS["claude-live"])["source"]
    _, cwd, shown = src.launch({**bad, "source": src, "id": "abc"}, False)
    assert cwd == bad["cwd"] and "\x1b" in cwd
    assert shown.startswith("cd -- $'/tmp/EVIL\\x1b]0;PWNED") and not _has_bad(shown), shown
    rtl_dir = "/tmp/\N{HEBREW LETTER ALEF}\N{RIGHT-TO-LEFT EMBEDDING}x"
    rtl = src.launch({**bad, "source": src, "id": "abc", "cwd": rtl_dir}, False)
    assert rtl[1] == rtl_dir and "\N{HEBREW LETTER ALEF}" in rtl[2] and not _has_bad(rtl[2]), rtl[2]
    try:                                            # no path holds a NUL, and exec cannot take one
        src.launch({**bad, "source": src, "id": "abc", "cwd": "/tmp/a\x00b"}, False)
        raise AssertionError("a NUL in the directory was accepted")
    except RuntimeError:
        pass
    # A Hermes profile name travels in the command: it is rendered the same way.
    import types
    from hopback.sources import Source
    hermes = types.SimpleNamespace(NAME="hermes", LABEL="Hermes", ASSISTANT="Hermes",
                                   resume_cmd=lambda root, r, yolo: ["hermes", "-p", "pro\x1b[2Jfile", "--resume", r["id"]])
    argv, _, shown = Source(paths.this_host(), hermes, HOME).launch({**bad, "id": "abc", "cwd": "/tmp"}, False)
    assert argv[2] == "pro\x1b[2Jfile" and "$'pro\\x1b[2Jfile'" in shown and not _has_bad(shown), shown

    # The picker: the list, the loading line, the warnings box, the preview
    # once it arrives, and the notifications.
    rows, total = load()

    def screen_text(app):
        return "\n".join(s.text for s in app.screen._compositor.render_strips())

    async def drive():
        app = cli.build_app([bad] + rows, "", False, total + 1, False, discover(),
                            warnings=[EVIL])
        async with app.run_test(size=(160, 50)) as p:
            await p.pause()
            assert "EVIL" in screen_text(app) and not _has_bad(screen_text(app))
            app._previews.clear()
            app._preview_key = None
            app.update_preview()                         # "loading…" for the hostile row
            await p.pause()
            assert "loading" in screen_text(app) or app._previews
            assert not _has_bad(screen_text(app)), "loading line"
            await until(p, lambda: app._previews, "preview")
            await p.pause()
            assert "EVIL" in screen_text(app) and not _has_bad(screen_text(app)), "preview"
            lv = app.query_one("#list")
            lv.set_lines([EVIL], 0)                      # no set_line after it
            assert not _has_bad("".join(s.text for s in lv.render_line(0)))
            # Notifications: rendered as plain text (no control character), and
            # never as markup, which would crash the toast or eat the text.
            from textual.widgets._toast import Toast
            cli.STATE_PROBLEMS.append(EVIL)
            with mock.patch.object(app, "notify") as note:   # the toast layer is not in the strips
                app.report_state()
            assert note.call_count == 1 and not _has_bad(note.call_args[0][0]), note.call_args
            texts = ["boom [/oops] tail", "a[/]b", "[a b]", "path /home/u/[a b]/c",
                     "[red]r[/red] [link=https://e.example]l[/link]", "[@click=app.quit_none]CLICK[/]"]
            cli.STATE_PROBLEMS.extend(texts)
            app.report_state()
            await p.pause(0.2)
            shown_texts = [Toast(n).render().plain for n in app._notifications]   # raises on markup errors
            assert shown_texts[-len(texts):] == texts, shown_texts
            app.warn("literal [/x]\x1b[2J", severity="error")
            await p.pause(0.2)
            last = [Toast(n).render().plain for n in app._notifications]
            assert "literal [/x] [2J" in last, last
    asyncio.run(drive())

    # main(): the line after the picker exits, the cwd warning, the foreign-owner
    # message, and --print-cd.
    real = row(IDS["claude-live"])
    res = _run_main(chosen={**real, "name": EVIL, "cwd": "/no/such/\N{RIGHT-TO-LEFT OVERRIDE}dir"})
    assert "EVIL" in res.err and not _has_bad(res.err) and res.exec.called, res.err
    res = _run_main(chosen={**real, "cwd": "/tmp/" + EVIL})        # resumed in the exact directory
    assert not _has_bad(res.err) and res.exec.called, res.err
    res = _run_main(chosen={**real, "cwd": "/tmp/a\x00b"})        # a NUL cannot be passed on: refused
    assert "refusing" in res.err and not _has_bad(res.err) and not res.exec.called, res.err
    res = _run_main("--print-cd", chosen={**real, "cwd": "/no/such/\N{HEBREW LETTER ALEF}"})
    assert "\N{HEBREW LETTER ALEF}" in res.out and not res.exec.called, (res.out, res.err)
    res = _run_main("--print-cd", chosen={**real, "cwd": "/no/such/dir\r\N{RIGHT-TO-LEFT OVERRIDE}"})
    assert res.out.startswith("cd -- $'/no/such/dir\\x0d\\xe2\\x80\\xae' && claude --resume "), res.out
    assert not _has_bad(res.out) and not res.exec.called
    res = _run_main("--print-cd", chosen={**real, "cwd": "/tmp/a\x00b"})   # a clean message, no traceback
    assert "refusing" in res.err and not res.out, (res.out, res.err)
    res = _run_main("--print-cd", chosen={**real, "cwd": "-"})
    assert res.out.startswith("cd -- - && "), res.out
    # A directory that really ends in a CR is resumed in exactly that directory.
    crdir = TMP / "crdir\r"
    crdir.mkdir()
    res = _run_main(chosen={**real, "cwd": str(crdir)})
    assert res.exec.called and res.chdir.call_args[0][0] == str(crdir), (res.err, res.chdir.call_args)
    # The paths that "are not owned by you" are file names another user made.
    here = Source(paths.this_host(), claude, HOME / ".claude" / "projects")
    res = _run_main(chosen={**real, "source": here, "cwd": "/tmp"},
                    foreign=["/tmp/claude-1/ev\x1b]0;x\x07\N{ZERO WIDTH SPACE}"])
    assert "not owned by you" in res.err and not _has_bad(res.err) and not res.exec.called, res.err


def test_id_lookup_and_doctor_reach_no_terminal_raw():
    import types
    from hopback.sources import Source
    T = 1_700_000_000
    root = TMP / "evilstore"
    names = ["ev\x1b]0;PWNED\x07\x1b[2Ja", "ev\x1b]0;PWNED\x07\x1b[2Jb", "evok", "evok2"]
    for n in names:
        _cost_session(root / "projects" / "-p", n, T, {"m": _usage(2.0)})
    src = Source(paths.this_host(), claude, root)
    # A unique safe prefix prints exactly the id; ids launch() would refuse are no hits.
    res = _run_main("--id", "evok2", sources=[src], tty=False)
    assert res.out == "evok2\n" and not _has_bad(res.err), res
    for prefix in ("evok", "ev"):                  # the two safe ids: listed on stderr
        res = _run_main("--id", prefix, sources=[src], tty=False)
        assert "2 sessions match" in res.err and "evok2" in res.err and not _has_bad(res.err), res.err
    res = _run_main("--id", "ev\x1b", sources=[src], tty=False)
    assert "no session id starts with" in res.err and not _has_bad(res.err) and res.out == "", res
    # --preview of an unusual id: its error line shows the id as a repr.
    res = _run_main("--preview", names[0], sources=[src], tty=False)
    assert not _has_bad(res.err) and not _has_bad(res.out), (res.out, res.err)
    with mock.patch.object(cli, "load_rows", return_value=([], 0, [])):    # found, but unreadable
        res = _run_main("--preview", names[0], sources=[src], tty=False)
    assert "could not be read" in res.err and not _has_bad(res.err), res.err
    # --doctor: a store whose path, or whose error text, holds an escape sequence.
    bad_root = Path("/no/such/p\x1b[2Jx")
    broken = types.SimpleNamespace(NAME="hermes", LABEL="Hermes", count=mock.Mock(side_effect=OSError(EVIL)))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.doctor([Source(paths.this_host(), claude, bad_root), Source(paths.this_host(), broken, root)])
    assert "p [2Jx" in out.getvalue() and "UNREADABLE" in out.getvalue(), out.getvalue()
    assert not _has_bad(out.getvalue()), out.getvalue()


def test_printed_command_round_trips_through_a_shell():
    """The --print-cd output is read by bash or zsh, so every string must come
    back byte for byte. The strings are fixed test data, never session data;
    each is only quoted as one word and printed."""
    import shutil as _sh
    import subprocess
    from hopback.fmt import shq
    samples = ["plain", "with space", "it's", "a'; true #\t", "q'\n'", "back\\slash", "$(not run) `x` $HOME", "tab\there",
               "cr\r", "esc\x1b[2J", "c1\x9b", "\N{RIGHT-TO-LEFT OVERRIDE}rlo", "\N{ZERO WIDTH SPACE}zw",
               "\N{HEBREW LETTER ALEF}\N{RIGHT-TO-LEFT EMBEDDING}x", "\N{TAG LATIN CAPITAL LETTER A}",
               "emoji \N{ZERO WIDTH JOINER} kept", "new\nline", "\\'\x01\\", "\N{GRINNING FACE} smile", "soft\N{SOFT HYPHEN}hyphen",
               "\udc80", "\udc41", "\ud83d", "a\ud83d\tb"]   # lone surrogates (valid JSON can make them)

    def raw_of(text):
        try:
            return text.encode("utf-8", "surrogateescape")
        except UnicodeEncodeError:
            return text.encode("utf-8", "surrogatepass")
    # A shell without $'..' (dash, sh) must see an inert literal, never live code.
    for shell in ("sh", "dash"):
        exe = _sh.which(shell)
        if not exe:
            continue
        mark = TMP / "injected"
        for text in [f"a'; touch {mark} #\t", f"q'\n'; touch {mark}; '", f"x\\'; touch {mark}; '\t", f"'\x1b'; touch {mark} #"]:
            q = shq(text)
            assert "'" not in q[2:-1], q
            got = subprocess.run([exe, "-c", f"cd -- {q} 2>/dev/null; printf %s {q} 2>/dev/null"],
                                 capture_output=True, timeout=30)
            assert not mark.exists(), (shell, text, q, got)
    for shell in ("bash", "zsh"):
        exe = _sh.which(shell)
        if not exe:
            continue
        for text in samples:
            q = shq(text)
            assert not _has_bad(q), (shell, q)
            got = subprocess.run([exe, "-c", f"printf %s {q}"], capture_output=True, timeout=30)
            assert got.returncode == 0 and got.stdout == raw_of(text), (shell, text, q, got)


def test_hiding_a_row_keeps_the_viewport():
    rows, total = load()

    async def drive():
        app = cli.build_app(rows, "", False, total, False, discover())
        async with app.run_test(size=(150, 30)) as p:
            await p.pause()
            lv = app.query_one("#list")
            for _ in range(lv.scrollable_content_region.height + 3):
                await p.press("down")
            for _ in range(3):
                await p.press("up")                      # the cursor mid-window, not at an edge
            await p.pause()
            top, index, n = int(lv.scroll_offset.y), lv.index, len(app._visible)
            assert top > 0 and index > top, (top, index)
            await p.press("ctrl+x")
            await rebuilt(app, p)
            await p.pause()
            assert len(app._visible) == n - 1
            assert (int(lv.scroll_offset.y), lv.index) == (top, index), \
                (int(lv.scroll_offset.y), lv.index, top, index)
    try:
        asyncio.run(drive())
    finally:
        cli.save_hidden(set())


def test_digits_type_in_the_search_box_outside_review():
    rows, total = load()

    async def drive():
        app = cli.build_app(rows, "", False, total, False, discover())
        async with app.run_test(size=(150, 30)) as p:
            await p.pause()
            app.query_one("#search").focus()
            await p.press("2", "0", "2", "6")
            assert app.query_one("#search").value == "2026"
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
            await rebuilt(app, p)
            await p.pause()
            assert app._visible == [victim] and len(lv.lines) == 1
            assert victim["name"][:20] in lv.lines[0] and lv.index == 0
            await p.press("f12")
            await rebuilt(app, p)
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


def test_cost_weights_per_model_and_ttl():
    wt = claude.weighted_tokens
    reads = {"claude-opus-5-5": .05, "claude-sonnet-5-5": .05, "claude-fable-5-1": .025,
             "claude-mythos-5-1": .025, "claude-opus-4-8": .1, "unknown": .1, None: .1}
    for model, mult in reads.items():
        assert abs(wt({"cache_read_input_tokens": 1000}, model) - 1000 * mult) < 1e-9, model
    assert wt({"output_tokens": 3}) == 15                     # output 5x, thinking included
    five = {"cache_creation_input_tokens": 100, "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0}}
    hour = {"cache_creation_input_tokens": 100, "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 100}}
    assert wt(five) == 125 and wt(hour) == 200
    assert wt({"cache_creation_input_tokens": 100}) == 125     # no cache_creation object: 5m
    # compaction call: zero top level, real numbers in iterations[]
    comp = {"input_tokens": 0, "output_tokens": 0, "iterations": [
        {"input_tokens": 10, "output_tokens": 2}, {"input_tokens": 5, "cache_read_input_tokens": 100}]}
    assert wt(comp, "claude-sonnet-5-5") == 10 + 10 + 5 + 5
    assert wt({"input_tokens": 7, "iterations": [{"input_tokens": 99}]}) == 7   # top level wins when present


def test_usage_by_model_counts_iterations_and_model_reads():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "s.jsonl"
        recs = [{"type": "assistant", "timestamp": "2026-10-09T00:00:0%dZ" % i, "message": {
            "id": "m%d" % i, "model": "claude-opus-5-5", "usage": u}}
            for i, u in enumerate([{"cache_read_input_tokens": 200},
                                   {"input_tokens": 0, "iterations": [{"output_tokens": 4}]}])]
        f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        got = claude.usage_by_model(f)
        assert abs(got["claude-opus-5-5"] - 30.0) < 1e-9, got   # 200 * 0.05 reads + 4 * 5 iteration output


def test_session_cost_calibration_returns_its_own_price():
    # A session whose recorded cost is the exact price: the rest of the session
    # (after the cost record) must be priced at that same rate, per call.
    price = {"in": 4e-6, "w1h": 8e-6, "read": .2e-6, "out": 20e-6}
    def usage(i, o, r, w):
        return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": r,
                "cache_creation_input_tokens": w,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": w}}
    def exact(u):
        return (u["input_tokens"] * price["in"] + u["output_tokens"] * price["out"]
                + u["cache_read_input_tokens"] * price["read"] + u["cache_creation_input_tokens"] * price["w1h"])
    first, second = usage(100, 50, 5000, 2000), usage(30, 80, 9000, 500)
    def rec(i, ts, u):
        return {"type": "assistant", "timestamp": ts, "message": {"id": i, "model": "claude-opus-5-5", "usage": u}}
    cost_rec = {"type": "cost-state", "totalCostUSD": exact(first), "modelUsage": {"claude-opus-5-5": {
        "inputTokens": 100, "outputTokens": 50, "cacheReadInputTokens": 5000,
        "cacheCreationInputTokens": 2000, "costUSD": exact(first)}}}
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "proj"
        proj.mkdir()
        f = proj / "s.jsonl"
        f.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in [
            rec("a", "2026-10-09T00:00:01Z", first), cost_rec,
            rec("b", "2026-10-09T00:00:09Z", second)]) + "\n")
        got, is_exact, _, _ = claude.session_cost(f)
    assert not is_exact
    assert abs(got - (exact(first) + exact(second))) < 1e-9, (got, exact(first) + exact(second))


def test_model_rates_come_from_per_call_usage_and_skip_old_sonnet_reads():
    def usage(r, w):
        return {"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": r,
                "cache_creation_input_tokens": w,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": w}}
    def session(proj, name, model, version, base, key=None):
        u = usage(1000, 100)
        w = 10 + 50 + 1000 * claude.read_mult(model) + 200          # 1h writes at 2x
        n = 10 + 10 + 1000 + 100
        recs = [{"type": "assistant", "version": version, "timestamp": "2026-10-09T00:00:01Z",
                 "message": {"id": "a", "model": model, "usage": u}},
                {"type": "cost-state", "totalCostUSD": base * w, "modelUsage": {key or model: {
                    "inputTokens": 10, "outputTokens": 10, "cacheReadInputTokens": 1000,
                    "cacheCreationInputTokens": 100, "costUSD": base * w}}}]
        (proj / (name + ".jsonl")).write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in recs) + "\n")
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "projects"
        proj = store / "p"
        proj.mkdir(parents=True)
        session(proj, "o", "claude-opus-5-5", "2.1.290", 3e-6, key="claude-opus-5-5[1m]")
        session(proj, "s_old", "claude-sonnet-5-5", "2.1.295", 9e-6)
        claude._RATES.clear()
        r = claude.model_rates({"claude-opus-5-5", "claude-sonnet-5-5"}, store)
        claude._RATES.clear()
    assert abs(r["claude-opus-5-5"] - 3e-6) < 1e-12, r      # derived per call, [1m] suffix folded
    assert "claude-sonnet-5-5" not in r, r                  # 2.1.295 or earlier: wrong recorded read price


_COMPACT = {"separators": (",", ":")}


def _src_store(d, sessions):
    """A store of exited sessions. Each: (name, model, version, base price, record-token
    scale, record key suffix, subagent bytes). One 1000-read / 100-1h-write call each."""
    store = Path(d) / "projects"
    proj = store / "p"
    proj.mkdir(parents=True)
    for i, (name, model, version, base, scale, suffix, sub) in enumerate(sessions):
        w = 10 + 50 + 1000 * claude.read_mult(model) + 200
        usage = {"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": 1000,
                 "cache_creation_input_tokens": 100,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 100}}
        rec = {"type": "assistant", "timestamp": "2026-10-09T00:00:01Z", "message": {
            "id": "a", "model": model, "usage": usage}}
        if version:
            rec["version"] = version
        cost = {"type": "cost-state", "totalCostUSD": base * w, "modelUsage": {model + suffix: {
            "inputTokens": 10, "outputTokens": 10, "cacheReadInputTokens": 1000 * scale,
            "cacheCreationInputTokens": 100, "costUSD": base * w}}}
        f = proj / (name + ".jsonl")
        f.write_text("\n".join(json.dumps(r, **_COMPACT) for r in (rec, cost)) + "\n")
        os.utime(f, (1000 + i, 1000 + i))                    # later in the list = newer
        if sub:
            (proj / name / "subagents").mkdir(parents=True)
            (proj / name / "subagents" / "x.jsonl").write_text("x" * sub)
    return store


def _rates(sessions, **patch):
    with tempfile.TemporaryDirectory() as d:
        store = _src_store(d, sessions)
        claude._RATES.clear()
        with mock.patch.multiple(claude, **patch) if patch else contextlib.nullcontext():
            r = claude.model_rates({"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}, store)
        claude._RATES.clear()
    return r


def test_rate_source_guards():
    O, S = "claude-opus-5-5", "claude-sonnet-5-5"
    ok = lambda r, m, b: abs(r.get(m, -1) - b) < 1e-12
    assert ok(_rates([("a", O, "2.1.300", 3e-6, 1.03, "", 0)]), O, 3e-6)           # 3% off: accepted
    assert O not in _rates([("a", O, "2.1.300", 3e-6, 1.10, "", 0)])               # 10% off: rejected
    assert ok(_rates([("a", S, "2.1.296", 3e-6, 1, "", 0)]), S, 3e-6)              # fixed version
    assert S not in _rates([("a", S, "2.1.295", 3e-6, 1, "", 0)])
    assert S not in _rates([("a", S, None, 3e-6, 1, "", 0)])                       # unknown version
    assert ok(_rates([("a", O, None, 3e-6, 1, "[1m]", 0)]), O, 3e-6)               # unknown version is fine off sonnet
    # size cap: aggregate fallback; budget: skipped, and NOT replaced by the unverified aggregate (L1)
    assert O in _rates([("a", O, "2.1.300", 3e-6, 1, "", 0)], SOURCE_MAX_BYTES=30)       # too big: aggregate fallback
    assert S not in _rates([("a", S, "2.1.300", 3e-6, 1, "", 0)], SOURCE_MAX_BYTES=30)   # never for sonnet-5-5
    assert O not in _rates([("a", O, "2.1.300", 3e-6, 1, "", 0)], SOURCE_BUDGET=30)
    assert not ok(_rates([("a", O, "2.1.300", 3e-6, 1, "", 5000)], SOURCE_MAX_BYTES=4000), O, 3e-6)   # subagent bytes count
    assert ok(_rates([("a", O, "2.1.300", 3e-6, 1, "", 5000)], SOURCE_MAX_BYTES=10**6), O, 3e-6)
    # at most SOURCE_PER_MODEL sources are read per model
    seen = []
    real = claude.usage_totals
    def counting(*a, **k):
        seen.append(1)
        return real(*a, **k)
    _rates([(n, O, "2.1.300", 3e-6, 1, "", 0) for n in "abcd"], usage_totals=counting)
    assert len(seen) == claude.SOURCE_PER_MODEL == 2, len(seen)


def test_fallback_rate_only_for_models_without_calls():
    # No assistant calls for the model in a readable transcript: the aggregate is the fallback,
    # with cache writes at 1.25x (helper models write 5m cache), never for sonnet-5-5.
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "projects"
        (store / "p").mkdir(parents=True)
        usage = {m: {"inputTokens": 10, "outputTokens": 10, "cacheReadInputTokens": 100,
                     "cacheCreationInputTokens": 8, "costUSD": 1.0}
                 for m in ("claude-haiku-5-5", "claude-sonnet-5-5")}
        (store / "p" / "h.jsonl").write_text(json.dumps(
            {"type": "cost-state", "totalCostUSD": 2.0, "modelUsage": usage}, **_COMPACT) + "\n")
        claude._RATES.clear()
        r = claude.model_rates({"claude-haiku-5-5", "claude-sonnet-5-5"}, store)
        claude._RATES.clear()
    assert abs(r["claude-haiku-5-5"] - 1.0 / (10 + 50 + 100 * .1 + 8 * 1.25)) < 1e-12, r
    assert "claude-sonnet-5-5" not in r, r


def test_usage_totals_survive_malformed_lines():
    good = {"type": "assistant", "timestamp": "2026-10-09T00:00:01Z", "message": {
        "id": "g", "model": "m", "usage": {"input_tokens": 7}}}
    def call(usage=None, message=None):
        return {"type": "assistant", "timestamp": "t", "message": message if message is not None else {
            "id": "b", "model": "m", "usage": usage}}
    bad = [json.dumps(call(usage="oops")), json.dumps(call(usage={"input_tokens": "9"})),
           json.dumps(call(usage={"iterations": 5})),
           json.dumps(call(usage={"input_tokens": 0, "iterations": [{"cache_creation": "x"}, 3]})),
           json.dumps(call(usage={"input_tokens": 1, "cache_creation": "x"})),
           '{"type":"assistant","timestamp":"t","message":{"id":"b","model":"m","usage":{"input_tokens":1' + "0" * 400 + '}}}',
           json.dumps(call(usage={"input_tokens": -1000000})),
           json.dumps(call(usage={"cache_creation_input_tokens": 10,
                                  "cache_creation": {"ephemeral_1h_input_tokens": 1000}})),
           json.dumps(call(message="a string")), json.dumps([1, 2, "usage assistant"]),
           '{"type":"assistant","usage":' + "[" * 100000 + '"usage"}',
           json.dumps({"type": "assistant", "timestamp": 5, "message": {"id": 3, "model": "m", "usage": {"input_tokens": 1}}})]
    with tempfile.TemporaryDirectory() as d:
        for i, line in enumerate(bad):
            f = Path(d) / ("s%d.jsonl" % i)
            f.write_text(line + "\n" + json.dumps(good) + "\n")
            assert claude.usage_totals(f) == {"m": (7, 7)}, (i, line[:60])
        # a call whose only usage is bad iterations counts as nothing, not as an error
        assert claude.weighted_tokens({"iterations": [{"input_tokens": "x"}]}) == 0


def test_own_rate_folds_1m_names_and_applies_the_gates():
    def run(key, version, scale=1):
        with tempfile.TemporaryDirectory() as d:
            store = _src_store(d, [("a", "claude-sonnet-5-5", version, 2e-6, scale, key, 0)])
            f = store / "p" / "a.jsonl"
            tail = {"type": "assistant", "timestamp": "2026-10-09T00:00:09Z", "message": {
                "id": "b", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 1000}}}
            with f.open("a") as fh:
                fh.write(json.dumps(tail, **_COMPACT) + "\n")
            claude._RATES.clear()
            cost, exact, _, _ = claude.session_cost(f)
            claude._RATES.clear()
        return cost, exact
    base = 2e-6 * (10 + 50 + 1000 * .05 + 200)
    c, e = run("[1m]", "2.1.300")
    assert not e and abs(c - (base + 1000 * 2e-6)) < 1e-12, c       # [1m] key folded
    assert abs(run("", "2.1.295")[0] - base) < 1e-12              # old sonnet: own rate refused, tail unpriced
    assert abs(run("", "2.1.300", scale=1.5)[0] - base) < 1e-12                   # tokens off by >5%: not calibrated


def test_version_is_the_first_records_field():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "v.jsonl"
        f.write_text(json.dumps({"type": "user", "message": {"content": '"version":"9.9.9"'}}) + "\n"
                     + json.dumps({"type": "x", "version": "2.1.296"}) + "\n")
        assert claude._version(f) == (2, 1, 296)
        f.write_text(json.dumps({"version": "2.1.296-beta"}) + "\n")
        assert claude._version(f) == (2, 1, 296)
        f.write_text("not json \"version\":\"1.2.3\"\n")
        assert claude._version(f) is None


def test_confirmed_rate_refuses_nonpositive_inputs():
    ok = {"costUSD": 2.0, "tokens": 100}
    assert claude._confirmed_rate("m", ok, (4.0, 100), None) == 0.5
    for u in ({"costUSD": 0, "tokens": 100}, {"costUSD": -2.0, "tokens": 100}, {"costUSD": 2.0, "tokens": 0}, None):
        assert claude._confirmed_rate("m", u, (4.0, 100), None) is None, u
    assert claude._confirmed_rate("m", ok, (0, 100), None) is None
    assert claude._confirmed_rate("m", ok, (-4.0, 100), None) is None


def test_session_cost_survives_a_cost_record_that_is_not_an_object():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "s.jsonl"
        f.write_text('[{"type":"cost-state"}]\n')
        assert claude.session_cost(f)[0] in (0, 0.0, None)


def test_rate_source_size_budget_and_cache_rules():
    O = "claude-opus-5-5"
    assert (claude.SOURCE_MAX_BYTES, claude.SOURCE_BUDGET) == (30 * 1024 * 1024, 400 * 1024 * 1024)
    # a sparse subagent file just over 30 MB: skipped; the cap raised 100x: read
    with tempfile.TemporaryDirectory() as d:
        store = _src_store(d, [("a", O, "2.1.300", 3e-6, 1, "", 1)])
        sub = store / "p" / "a" / "subagents" / "x.jsonl"
        with sub.open("r+b") as fh:
            fh.truncate(30 * 1024 * 1024 + 1)
        claude._RATES.clear()
        too_big = claude.model_rates({O}, store)[O]
        claude._RATES.clear()
        with mock.patch.object(claude, "SOURCE_MAX_BYTES", 100 * 30 * 1024 * 1024):
            assert claude.model_rates({O}, store)[O] != too_big   # per-call rate, not the aggregate
        claude._RATES.clear()
    # a budget that fits one session only: the second is not read
    seen = []
    real = claude.usage_totals
    def counting(*a, **k):
        seen.append(1)
        return real(*a, **k)
    two = [(n, O, "2.1.300", 3e-6, 1, "", 0) for n in "ab"]
    with tempfile.TemporaryDirectory() as d:
        store = _src_store(d, two)
        size = (store / "p" / "a.jsonl").stat().st_size
        for budget, want in ((size + 10, 1), (100 * size, 2)):
            seen.clear()
            claude._RATES.clear()
            with mock.patch.object(claude, "SOURCE_BUDGET", budget), mock.patch.object(claude, "usage_totals", counting):
                claude.model_rates({O}, store)
            assert len(seen) == want, (budget, len(seen))
        # a transient read error is returned but never cached
        claude._RATES.clear()
        def eio(path, marker, chunk=0, errors=None):
            errors.append(OSError(5, "EIO"))
            return None, None
        with mock.patch.object(claude, "last_line_with", eio):
            claude.model_rates({O}, store)
        assert store not in claude._RATES
        claude._RATES.clear()


def test_session_cost_validates_scalar_totals():
    for bad in ('"5"', "NaN", "Infinity", "true", "-3"):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.jsonl"
            f.write_text('{"type":"cost-state","totalCostUSD":%s,"totalLinesAdded":%s,"totalLinesRemoved":%s}\n'
                         % (bad, bad, bad))
            cost, _, added, removed = claude.session_cost(f)
            assert cost in (0, 0.0, None) and added is None and removed is None, (bad, cost, added, removed)


def test_too_big_transcript_falls_back_to_aggregate_except_sonnet_5_5():
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "projects"
        usage = {m: {"inputTokens": 10, "outputTokens": 10, "cacheReadInputTokens": 100,
                     "cacheCreationInputTokens": 8, "costUSD": 1.0}
                 for m in ("claude-opus-5", "claude-sonnet-5-5")}
        (store / "p").mkdir(parents=True)
        (store / "p" / "h.jsonl").write_text(json.dumps(
            {"type": "cost-state", "totalCostUSD": 2.0, "modelUsage": usage}, **_COMPACT) + "\n")
        claude._RATES.clear()
        with mock.patch.object(claude, "SOURCE_MAX_BYTES", 1):
            r = claude.model_rates({"claude-opus-5", "claude-sonnet-5-5"}, store)
        claude._RATES.clear()
    assert "claude-opus-5" in r and "claude-sonnet-5-5" not in r, r


def test_exec_leaves_no_leaked_semaphores():
    # The preview pool, torn down as on_unmount does, then exec into the agent:
    # without the cleanup the resource tracker warns when the agent exits.
    import subprocess
    script = (
        "from hopback import cli\n"
        "pool = cli.preview_pool()\n"
        "assert pool is not None\n"
        "pool.submit(int).result()\n"
        "for p in list(pool._processes.values()): p.kill()\n"
        "pool.shutdown(wait=False, cancel_futures=True)\n"
        "cli.exec_agent(['true'])\n")
    root = str(Path(__file__).resolve().parent.parent)
    out = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True,
                         text=True, timeout=60, env={**os.environ, "PYTHONPATH": root})
    assert out.returncode == 0, out.stderr
    # The tracker writes its warning after the exec'd process exits; give it a moment.
    import time
    time.sleep(0.5)
    assert "leaked semaphore" not in out.stderr, out.stderr


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                setup_function()
                fn()
                print(f"ok    {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}: {exc.__class__.__name__}: {exc}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if failed else 0)

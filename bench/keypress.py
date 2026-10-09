"""Measure how fast the picker responds to cursor moves, on the real stores.

Run from the repo root with the installed venv's python:
    ~/.local/share/hopback/venv/bin/python bench/keypress.py [--n 300] [--presses 30]
                                                             [--cache PATH | --no-cache]

Three numbers:
  preview build   adapter details() per row, called directly (no UI)
  cpu per press   process_time around Pilot.press("down") + pause(), with the
                  preview pane shown and hidden. Wall clock is useless here:
                  Pilot waits for the app to go idle, a fixed ~150 ms per press
                  that has nothing to do with what a user sees. process_time is
                  process-wide, so with a cold cache it also counts background
                  preview threads; read the event-loop gap for UI freezes.
  event-loop gap  a 5 ms set_interval ticker inside the app while the cursor is
                  hammered with app.action_cursor_down/up (no Pilot waits). The
                  worst gap is the longest the UI froze.

Works against any revision: if hopback.previewcache exists, the picker gets a
Previewer (persisting to --cache, default a throwaway file, so a first run
is cold and a second is warm); otherwise the app's own preview code is used.
"""
import argparse
import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hopback import cli  # noqa: E402
from hopback.sources import discover  # noqa: E402

try:
    from hopback import previewcache
except ImportError:
    previewcache = None


def ms(xs):
    xs = sorted(xs)
    return (f"median {statistics.median(xs) * 1e3:.0f}  p90 {xs[int(len(xs) * .9)] * 1e3:.0f}"
            f"  max {xs[-1] * 1e3:.0f} ms")


def make_app(rows, total, sources, cache, no_cache):
    kw = {}
    if previewcache is not None:
        kw["previewer"] = (previewcache.Previewer(persist=False) if no_cache
                           else previewcache.Previewer(path=Path(cache)))
    return cli.build_app(rows, "", False, total, False, sources, **kw), kw.get("previewer")


async def cpu_per_press(make, presses, hide_preview):
    app, _ = make()
    async with app.run_test(size=(160, 50)) as p:
        await p.pause()
        if hide_preview:
            app.action_toggle_preview()
            await p.pause()
        ts = []
        for _ in range(presses):
            t = time.process_time()
            await p.press("down")
            await p.pause()
            ts.append(time.process_time() - t)
        return ts


async def loop_gaps(make):
    app, pv = make()
    async with app.run_test(size=(160, 50)) as p:
        await p.pause()
        gaps, last = [], [time.perf_counter()]

        def tick():
            now = time.perf_counter()
            gaps.append(now - last[0])
            last[0] = now

        app.set_interval(0.005, tick)
        for i in range(60):   # down 12, up 8, like a wheel spun back and forth
            (app.action_cursor_down if i % 20 < 12 else app.action_cursor_up)()
            await asyncio.sleep(0.01)
        # Keep measuring while background preview work finishes.
        for _ in range(200):
            await asyncio.sleep(0.05)
            if pv is None and len(gaps) > 600:
                break
            if pv is not None and all(pv.get(r) for r in app._visible):
                break
        return gaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300, help="rows loaded per store")
    ap.add_argument("--presses", type=int, default=30)
    ap.add_argument("--cache", help="previews.json to use (run twice: cold, then warm)")
    ap.add_argument("--no-cache", action="store_true", help="in-memory previewer only")
    args = ap.parse_args()
    cache = args.cache or str(Path(tempfile.mkdtemp(prefix="hopback-bench-")) / "previews.json")

    sources = discover()
    t = time.perf_counter()
    rows, total, _ = cli.load_rows(sources, args.n, False, False, include_teams=True,
                                   limit_counts_visible=True)
    print(f"load_rows: {time.perf_counter() - t:.2f}s, {len(rows)} rows, "
          f"previewcache={'yes' if previewcache else 'no'}, cache={cache}")

    ts = []
    for r in rows[:40]:
        t = time.perf_counter()
        r["source"].adapter.details(r["source"].root, r["id"])
        ts.append(time.perf_counter() - t)
    print(f"preview build (details(), first 40 rows): {ms(ts)}")

    def make():
        return make_app(rows, total, sources, cache, args.no_cache)

    for hide in (False, True):
        ts = asyncio.run(cpu_per_press(make, args.presses, hide))
        print(f"cpu per press, preview {'off' if hide else 'on '}: {ms(ts)}")
    gaps = asyncio.run(loop_gaps(make))
    print(f"event-loop gaps while hammering: {ms(gaps)}  (n={len(gaps)})")


if __name__ == "__main__":
    main()

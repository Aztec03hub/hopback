# hopback handoff (updated 2026-10-08)

Read this first, then `docs/PLAN.md`. Verify state with `git log` before trusting any of it.

## Where things are
- Repo: `~/claude_projects/hopback` (folder was `claude-sessions`), GitHub https://github.com/Aztec03hub/hopback (PUBLIC, renamed from claude-sessions; old URLs redirect). Last pushed commit at writing: `04e140e` (merge of PR #1, spawn detection and the Hidden/Review views).
- Installed for Phil via `bash install.sh`: venv `~/.local/share/hopback/venv`, symlinks `~/.local/bin/hopback` and `~/.local/bin/claude-sessions`. Re-run `install.sh` after every code change so his local copy matches, then `cmp` an installed file against the repo: pip can skip a same-version local reinstall.
- Old install `~/.local/share/claude-sessions/` is unused leftover; not deleted (Phil hasn't asked).
- Origin: tool was first built in Claude session `0439c59b-773a-43f0-91f6-9faa38930a45` (2026-08-29, plain print -> fzf -> Textual rewrite at 02:48Z on Phil's request).
- Spawn detection, the parser comparison and the Review view were built in session `7c75cdd3-43ea-492e-915a-248d4523a0b8` ("hopback development", 2026-10-08). Scratch work for it lives OUTSIDE the repo in `~/claude_projects/hopback-review/` (parser benchmark in `parser-bench/`, its `REPORT.md`, the old hand-written parser kept as `parser-bench/shellparse.hand-written.py`).

## Layout
- `hopback/cli.py` picker + main; `fmt.py`; `paths.py` (WSL/Windows paths, PowerShell quoting); `readers.py` (backwards line reader, tolerant JSON, read-only SQLite); `sources.py` (Source = host+adapter+root, discovery incl. `/mnt/c/Users/<you>`, Windows resume via pwsh with id allowlist + `-ErrorAction Stop`; pickles via `__reduce__` for the preview process pool).
- `hopback/adapters/{claude,codex,hermes}.py`; contract documented in `adapters/__init__.py` (ROLES, AGENT, SCHEDULED, LEFTOVER sets). `collect` may return a third item, a list of problems shown above the list.
- `hopback/launches.py`: which Claude Code sessions another session's Claude started (role `spawn`). Three-rule test in its docstring; incremental byte-offset cache in `$XDG_CACHE_HOME/hopback/`; ripgrep for the cold scan, a process pool otherwise. Stores evidence with each verdict; `explain()` only reads it.
- `hopback/shellparse.py`: "does this shell command start a new claude?" Only the decision lives here; `self_check()` is its regression table.
- `hopback/_vendor/bashtree/`: a pinned copy of the shared bashtree library (public: https://github.com/Aztec03hub/bashtree, local `~/claude_projects/bashtree`), vendored at commit c1ba473, the tree-sitter-bash layer shared with the bash-write-guard hook. Never edit the copy: change bashtree (its COORDINATION.md says how; the guard's session reviews), then re-vendor: copy bashtree's `bashtree/*.py` over `hopback/_vendor/bashtree/` and set `Source commit:` in `VENDORED` to the new full sha. tree-sitter-bash was chosen by measurement over the old hand-written lexer and bashlex (hopback-review/parser-bench/REPORT.md).
- State: `~/.local/state/hopback/hidden.json` (CTRL-X hides) and `review.json` (Review verdicts, by session id). A damaged one is moved aside as `<name>.damaged-<time>-<pid>`, never overwritten.
- `tests/demo_store.py` = fake multi-harness multi-host store (used by tests AND screenshots). `tests/test_hopback.py` (22 tests; run with the venv python). `tests/mutate_launches.py` removes each guard from a temp copy and needs the tests to fail (every mutant "killed", well under 2 min).
- Screenshots: `docs/make_screenshots.py` then `bash docs/render_pngs.sh` (headless Chrome). Always OPEN each PNG and grep SVGs for `csdm|cswn|plafayette` before committing; repo is public.

## Decisions Phil made
- Name **hopback** (recall = Windows Recall + PyPI taken; ai-sessions = a direct competitor on PyPI).
- Default shows every host, clearly labelled; Hermes cron runs get their own toggle (CTRL-R).
- Only CLI/TUI agents; no Claude Desktop / Cursor IDE.
- README must say plainly: built for WSL; macOS untested; native Windows unsupported.
- Phil granted full autonomy while away ("make all executive decisions").
- Parser is tree-sitter-bash (2026-10-08), after measuring all three on 162,750 real commands. It's a personal project, so license wasn't a factor.
- The role is `spawn`, not `bot`: subagents (`sub`) and teammates (`team`) are bots too, just recorded ones. `spawn` is the kind that has to be inferred.
- Detection errs towards NOT marking: a false positive hides a session the user started.
- Never "fix" a crash by swallowing the exception; every failure is shown (problems list, notify, preview field) or retried.
- Scripts and bots must not pass `claude -n/--name` (it makes a session look named by Phil).

## Facts measured on this laptop
- Stores (2026-10-02): claude·wsl ~4,475 sessions; codex·wsl 104 (84 subagents); hermes default 290 (204 cron) + profiles aletheia-scribe 171, aletheia-sol 100, writer 6; claude·win 1; codex·win 2 (tagged `ide`, thread_source onboarding_checklist).
- `claude` and `codex` are NOT installed on Windows, so resuming the Windows sessions correctly prints "not installed on Windows".
- Codex has two rollout formats: old `user_message`/`agent_message`, new `item_completed` (UserMessage/AgentMessage). `response_item role=user` is injected context; never show it.
- Codex records no cost; Hermes cost columns are empty for Phil's provider.
- Claude cost: `cost-state` record is written only on process exit; estimate for the rest back-tested at ~6.5% median error (11 pairs, a few ~85% off).
- Spawn detection (2026-10-08): cold scan of ~10 GB of transcripts 2.4 s (ripgrep), warm 0.04 s; 96 sessions marked `spawn`, 32 of which would leave the main list. Duplicate sessions in the list were background-job hand-offs (`continued-in`), now folded as role `copy`.
- tree-sitter-bash grammar limits and the caps that handle them (heredocs, unclosed `a=(`, `! { a; }`) are listed in bashtree's README and `core.py`; none of the affected real commands is a launch.
- Textual: SessionList paints the rows itself (a ScrollView and its line API); `set_lines` is synchronous, so a rebuild cannot be cancelled part way. The ticket still matters: `_current()` refuses while a rebuild is requested but not shown, and an overtaken rebuild returns without touching the list. (The old ListView froze when an `exclusive=True` worker was cancelled inside `clear()`/`extend()`; that is why it was replaced.) Tests wait for a rebuild by polling `_shown_ticket == _refresh_ticket`, never `app.workers.wait_for_complete()`, which also gathers the preview worker the app cancels itself.
- Known differences from main's ListView picker (perf-s2): (1) drag-selecting text over the rows no longer works inside the app, because rows are painted with the line API and a ScrollView has no selectable text (terminal-native Shift+drag still works). A cheap partial fix exists, not applied: `SessionList.get_selection` returning `selection.extract("\n".join(self.lines)), "\n"` copies the text without painting a highlight. (2) Cost rates are read once per process: a model first used after startup shows no rate until the next launch.

## Next
- Done 2026-10-08: Phil reviewed all 96 `spawn` marks and accepted every one (no false positives); merged as PR #1.
- Phase 6 adapters (Qwen Code, Copilot CLI, Goose, ...): blocked on having a real install to inspect; don't build from docs alone.
- Group-by-directory view (PLAN Option D).
- Optional: list cache for startup speed; LICENSE (none yet, so legally all-rights-reserved; ask Phil).
- Process rules: a fresh reviewer on every change before push, repeated until a round is clean; no Co-Authored-By trailers; no em dashes in prose.

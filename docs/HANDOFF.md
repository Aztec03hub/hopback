# hopback handoff (written 2026-10-02, before a context compaction)

Read this first, then `docs/PLAN.md`. Verify state with `git log` before trusting any of it.

## Where things are
- Repo: `~/claude_projects/hopback` (folder was `claude-sessions`), GitHub https://github.com/Aztec03hub/hopback (PUBLIC, renamed from claude-sessions; old URLs redirect). Last pushed commit at writing: `80691b4`.
- Installed for Phil via `bash install.sh`: venv `~/.local/share/hopback/venv`, symlinks `~/.local/bin/hopback` and `~/.local/bin/claude-sessions`. Re-run `install.sh` after every code change so his local copy matches.
- Old install `~/.local/share/claude-sessions/` is unused leftover; not deleted (Phil hasn't asked).
- Origin: tool was first built in Claude session `0439c59b-773a-43f0-91f6-9faa38930a45` (2026-08-29, plain print -> fzf -> Textual rewrite at 02:48Z on Phil's request).

## Layout
- `hopback/cli.py` picker + main; `fmt.py`; `paths.py` (WSL/Windows paths, PowerShell quoting); `readers.py` (backwards line reader, tolerant JSON, read-only SQLite); `sources.py` (Source = host+adapter+root, discovery incl. `/mnt/c/Users/<you>`, Windows resume via pwsh with id allowlist + `-ErrorAction Stop`).
- `hopback/adapters/{claude,codex,hermes}.py`; contract documented in `adapters/__init__.py` (ROLES, AGENT, SCHEDULED sets).
- `tests/demo_store.py` = fake multi-harness multi-host store (used by tests AND screenshots). `tests/test_hopback.py` (12 tests; run with the venv python).
- Screenshots: `docs/make_screenshots.py` then `bash docs/render_pngs.sh` (headless Chrome). Always OPEN each PNG and grep SVGs for `csdm|cswn|plafayette` before committing; repo is public.

## Decisions Phil made
- Name **hopback** (recall = Windows Recall + PyPI taken; ai-sessions = a direct competitor on PyPI).
- Default shows every host, clearly labelled; Hermes cron runs get their own toggle (CTRL-R).
- Only CLI/TUI agents; no Claude Desktop / Cursor IDE.
- README must say plainly: built for WSL; macOS untested; native Windows unsupported.
- Phil granted full autonomy while away ("make all executive decisions").

## Facts measured on this laptop
- Stores: claude·wsl ~4,475 sessions; codex·wsl 104 (84 subagents); hermes default 290 (204 cron) + profiles aletheia-scribe 171, aletheia-sol 100, writer 6; claude·win 1; codex·win 2 (tagged `ide`, thread_source onboarding_checklist).
- `claude` and `codex` are NOT installed on Windows, so resuming the Windows sessions correctly prints "not installed on Windows".
- Codex has two rollout formats: old `user_message`/`agent_message`, new `item_completed` (UserMessage/AgentMessage). `response_item role=user` is injected context; never show it.
- Codex records no cost; Hermes cost columns are empty for Phil's provider.
- Claude cost: `cost-state` record is written only on process exit; estimate for the rest back-tested at ~6.5% median error (11 pairs, a few ~85% off).
- Startup ~3-4.5 s, almost all Claude file scanning (pre-existing).

## Next (not started)
- Phase 6 adapters (Qwen Code, Copilot CLI, Goose, ...): blocked on having a real install to inspect; don't build from docs alone.
- Group-by-directory view (PLAN Option D).
- Optional: list cache for startup speed; LICENSE (none yet, so legally all-rights-reserved; ask Phil).
- Process rules: reviewer on every change before push; no Co-Authored-By trailers; no em dashes in prose.

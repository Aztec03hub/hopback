# Local research: Codex CLI and Hermes Agent session stores

Probed 2026-10-01 on this WSL box. Read-only (SQLite opened with `mode=ro`; no sqlite3 CLI is installed, so Python `sqlite3` was used). Versions: `codex-cli 0.156.1` (`codex --version`), `Hermes Agent v0.20.0 (2026.8.3)` (`hermes --version`).

## 1. OpenAI Codex CLI

### Storage
- Home: `~/.codex` (`$CODEX_HOME`, per https://learn.chatgpt.com/docs/developer-commands?surface=cli, the redirect target of developers.openai.com/codex/cli/reference).
- Transcripts: `~/.codex/sessions/YYYY/MM/DD/rollout-<local-ts>-<uuid>.jsonl`. 104 files on disk, 1.54 GB total (`codex doctor`), largest 817 MB.
- Index/metadata: `~/.codex/state_5.sqlite`, table `threads` (104 rows, one per rollout; 0 missing files, 0 orphan files). `threads.rollout_path` is the absolute path to the jsonl.
- `~/.codex/session_index.jsonl`: only 7 lines, keys `id, thread_name, updated_at`. A partial rename log, NOT authoritative; use `threads.name`.
- `~/.codex/history.jsonl`: 780 lines, keys `session_id, ts, text`. Flat prompt history, one row per user prompt.
- `~/.codex/thread_history_1.sqlite`: for sessions with `history_mode='paginated'` (27 of 104), tables `thread_turns` (771 rows; has `rollout_byte_offset`) and `thread_items` (95,022 rows; `item_json`, `item_type`). The rollout jsonl is still written for these sessions (checked: 2 MB and 29 MB files with full event streams), so jsonl parsing still works.
- Other DBs (`logs_2`, `queue_1`, `goals_1`, `memories_1`) are not needed.

### `threads` columns (state_5.sqlite)
`id, rollout_path, created_at (epoch s), updated_at, created_at_ms, updated_at_ms, recency_at(_ms), source, thread_source, model_provider, model, reasoning_effort, cwd, title, name, preview, first_user_message, git_sha, git_branch, git_origin_url, cli_version, sandbox_policy (JSON), approval_mode, tokens_used, has_user_event, archived, archived_at, is_pinned, agent_nickname, agent_role, agent_path, history_mode ('legacy'|'paginated'), memory_mode, project_id, originator, ...`

### Rollout jsonl record shapes (keys only)
Every line: `{timestamp, ordinal, type, payload}`. Types seen (type / payload.type):
- `session_meta`: payload `session_id, id, parent_thread_id, timestamp, cwd, originator, cli_version, source, thread_source, agent_nickname, agent_role, agent_path, model_provider, base_instructions, history_mode, multi_agent_version, context_window, git{commit_hash,branch,repository_url}`. Line 1 of each file.
- `turn_context`: `turn_id, cwd, approval_policy, sandbox_policy, model, effort, collaboration_mode, ...`
- `event_msg/task_started`: `turn_id, started_at, model_context_window`
- `event_msg/task_complete`: `turn_id, last_agent_message, started_at, completed_at, duration_ms` (last assistant message lives here)
- `event_msg/token_count`: `info.total_token_usage{input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens, total_tokens}, last_token_usage, model_context_window`, `rate_limits`
- `event_msg/item_completed`: `item{type: UserMessage|AgentMessage|Reasoning|CommandExecution|FileChange|Extension}` (UserMessage keys `type,id,content`)
- `response_item/message`: `role` developer|user|assistant, `content[]` of `input_text`/`output_text`
- `response_item/{reasoning,function_call,function_call_output,custom_tool_call,custom_tool_call_output,agent_message}`
- `token_usage_record`, `compacted`, `world_state`, `inter_agent_communication_metadata`, `event_msg/thread_settings_applied`

### Per-session field mapping
| Need | Source |
|---|---|
| id | `threads.id` (UUIDv7; also in filename and `session_meta.payload.id`) |
| title | `threads.title`; in all 104 rows equals `first_user_message` (auto) |
| user-renamed name | `threads.name` (5 non-null, all among the 20 user threads); `session_index.jsonl.thread_name` mirrors some |
| cwd | `threads.cwd` (7 distinct, all POSIX paths) |
| timestamps | `created_at`/`updated_at` epoch s (also `_ms`); range 2026-07-22..2026-09-25 |
| model | `threads.model` (gpt-5.6-sol 68, gpt-5.6-terra 22, gpt-6-astra 13, gpt-5.6-luna 1) |
| git branch | `threads.git_branch` (+ `git_sha`, `git_origin_url`) |
| first user prompt | `threads.first_user_message` (clean: 0 rows start with `<`) |
| last user prompt | scan rollout for `event_msg/item_completed` with `item.type==UserMessage`, or `history.jsonl` by `session_id`. Do NOT use `response_item/message role=user`: the first user messages are injected context (`<recommended_plugins>`, `# AGENTS.md instructions`, `<environment_context>` as separate `input_text` parts) |
| last assistant message | last `event_msg/task_complete.payload.last_agent_message` |
| tokens | `threads.tokens_used` (sum 5.78e9 over user threads, 1.13e9 over subagent threads); detail in last `token_count.info.total_token_usage` |
| cost | NOT recorded anywhere seen (tokens only); would need a price table |
| subagent marker | `threads.thread_source`: `user` 20, `subagent` 84. For subagents `source` is a JSON string `{"subagent":{"thread_spawn":{"parent_thread_id","depth","agent_path","agent_nickname","agent_role"}}}`; table `thread_spawn_edges(parent_thread_id, child_thread_id, status)` has 84 rows. User sessions have `source` `cli` (15) or `exec` (5) |

### Resume
- `codex resume [SESSION_ID] [PROMPT]`; ID is UUID or session name (`codex resume --help`). `--last` = most recent, `--all` disables cwd filtering, `--include-non-interactive` includes `exec` sessions.
- `-C/--cd <DIR>` sets the working root. Docs: if the current dir differs from the session's, Codex prompts which dir to use; config `tui.resume_cwd = "current"|"session"` sets the default (doc page above; NOT tested here). So resume need not run from the original cwd, but chdir plus `-C <cwd>` is the safe non-interactive choice.
- Yolo equivalent: `--dangerously-bypass-approvals-and-sandbox` (listed in `codex resume --help`; alternatives `-a never -s danger-full-access`, `--approve-for-me`). Command: `codex resume <id> --dangerously-bypass-approvals-and-sandbox`.
- Related: `codex fork [ID]`, `codex archive|unarchive|delete <id-or-name>`, `codex exec resume`.

### Parsing hazards
- 3 lines in 2 files are invalid JSON ("Invalid control character"): line 956 of `sessions/2026/08/28/...01a04a40-be27-7071-8424-b2b5fe5527ce.jsonl`-style file (name ends `01a04a40-be27...jsonl`), lines 18195 and 18225 of the `...019ffd51-de70...jsonl` file. Use `json.loads(line, strict=False)` or skip bad lines.
- Files up to 817 MB: never read whole; prefer SQLite and tail-scan for last messages.
- `history_mode` legacy vs paginated; both keep jsonl.
- `has_user_event` is 0 in all 104 rows, so do not use it to detect empties.
- WAL-mode DB in use; open read-only and tolerate locks. The schema is versioned (`state_5`), expect drift.

## 2. Hermes Agent (Nous Research)

### Storage
- Home: `~/.hermes`. Primary store `~/.hermes/state.db` (SQLite WAL, 342 MB). `hermes sessions --help`: "View and manage the SQLite session store".
- Profiles each have their own DB: `~/.hermes/profiles/<profile>/state.db` (three profiles here, with 100, 171 and 6 sessions).
- `~/.hermes/sessions/` holds only `sessions.json` (gateway routing map) and `request_dump_*.json` debug dumps, not transcripts. `hermes sessions export --format jsonl` produces JSONL on demand.
- Tables in `state.db`: `sessions` (290), `messages` (82,803), `session_model_usage` (365), `system_prompts`, `gateway_routing`, `delivery_obligations`, FTS tables.
- Source for reference: `~/.hermes/hermes-agent/hermes_state.py`, `hermes_cli/main.py`.

### Record shapes (keys only)
`sessions`: `id, source, user_id, session_key, chat_id, chat_type, thread_id, display_name, origin_json, model, model_config (JSON incl. yolo_mode), system_prompt, parent_session_id, started_at (REAL epoch), ended_at, end_reason, message_count, tool_call_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, cwd, git_branch, git_repo_root, billing_provider, billing_mode, estimated_cost_usd, actual_cost_usd, cost_status, cost_source, title, title_source, last_activity_at, last_activity_description, api_call_count, profile_name, archived, pinned, last_read_at, ...`
`messages`: `id, session_id, role (user|assistant|tool|session_meta), content, tool_call_id, tool_calls, tool_name, timestamp, token_count, finish_reason, reasoning*, active, compacted, display_kind (NULL|'hidden'), display_metadata, ...`

### Per-session field mapping
| Need | Source |
|---|---|
| id | `sessions.id`, e.g. `20260911_104936_c44700` (cli) |
| title | `sessions.title`; `title_source` = `user` (explicit; 204, all cron), `llm` 37, `derived` 7, NULL 42 |
| rename | `hermes sessions rename <id> <title>` |
| cwd | `sessions.cwd` (NULL for 3 of 43 cli, and all cron/telegram) |
| timestamps | `started_at`, `ended_at`, `last_activity_at` (REAL epoch s; last_activity_at NULL on 1 row) |
| model | `sessions.model` (per-task detail in `session_model_usage`) |
| git branch | `git_branch` (0 non-null in main DB; `git_repo_root` exists) |
| first/last user prompt | `messages` where `role='user'` ordered by `id`; skip `display_kind='hidden'` |
| last assistant message | last `messages` row `role='assistant'` with non-empty `content` (empty ones are tool-call turns, many hidden) |
| tokens | `input_tokens, output_tokens, cache_*_tokens, reasoning_tokens` on `sessions` |
| cost | `estimated_cost_usd`/`actual_cost_usd`; here 0.0 or NULL (`cost_status='unknown'` for 282 of 290; local/custom endpoints). Treat as unavailable |
| subagent marker | `source='subagent'` (41) with `parent_session_id` set (42 rows have a parent in total; 1 is a telegram child). Compression also forks a child via `parent_session_id`; parent may then have `message_count=0`; `resolve_resume_session_id` (`hermes_state.py:8380`) redirects resume to the descendant tip |

### Resume
- `hermes --resume <ID|title>` or `hermes chat --resume/-r <ID>`; `-c/--continue [NAME]` resumes by name or the most recent; `hermes sessions browse` is an interactive picker (`hermes --help`, `hermes chat --help`).
- Resume restores cwd itself: `hermes_cli/main.py:2606-2625` does `os.chdir(sessions.cwd)` unless `--no-restore-cwd` or `--worktree`; a missing dir only warns. `--in DIR` overrides. So the launcher does not strictly need to chdir, but doing it is harmless.
- Yolo equivalent: `--yolo` ("Bypass all dangerous command approval prompts"). Also `--accept-hooks`.
- Profile sessions: live in separate DBs. Resuming them presumably needs `hermes -p <profile>` or `HERMES_HOME`; NOT tested (check `hermes profile --help`).

### Counts and hazards
- Main DB: 290 sessions: cron 204, cli 43, subagent 41, telegram 2. Only cli and telegram are interactive; the 204 cron rows are noise (titled, no cwd). Profile DBs add 277 sessions (cli 95+168+6, kanban 5, subagent 3).
- All `archived=0` in every DB. Subagents have no title (0 of 41).
- `started_at` is REAL not int.
- DB is WAL and in use by a gateway (`gateway.pid`, `-wal` present): open with `mode=ro` and expect busy timeouts. Schema is versioned (`optimize-storage` mentions a "v23 layout"), expect drift.
- `messages` has `session_meta` role rows (2) that are not chat turns.
- One session has 41,240 messages (`20260828_123324_977999`); always query with LIMIT/ORDER BY.

## 3. Implementation notes for claude-sessions
- Both stores are SQLite-first. Use the DB for listing, rollouts / `messages` only for last-message lookups.
- Normalized record: `agent, id, title, renamed, cwd, started, last_active, model, branch, tokens, cost|None, role(lead/child), parent_id, resume_argv`.
- Resume argv: Claude `claude --resume <id>`; Codex `codex resume <id> [-C cwd] [--dangerously-bypass-approvals-and-sandbox]`; Hermes `hermes --resume <id> [--yolo]` (profile sessions likely need `-p`).
- Hide by default: Codex `thread_source='subagent'` and `source='exec'`; Hermes `source IN ('cron','subagent','kanban')`.

# Session storage and resume: Gemini CLI, Qwen Code, OpenCode, Crush (+ related CLIs)

Researched 2026-10-01 from GitHub source (read via the GitHub API / raw files) and official docs. Nothing was installed or run, so every path and schema below is read from source or docs, not observed on a real machine. Where a claim rests on docs or a web search rather than source, it is flagged.

## Summary

| CLI | Persists + resumes | Store | Format | Per-project or global | cwd recoverable | Cost / tokens | Feasibility |
|---|---|---|---|---|---|---|---|
| Qwen Code | yes | `~/.qwen/projects/<sanitized-cwd>/chats/<id>.jsonl` | JSONL, near-clone of Claude Code's format | per-project dir, but one root so global scan is trivial | yes (`cwd` field per record) | tokens only (`usageMetadata`), no cost | Easy |
| Gemini CLI | yes | `~/.gemini/tmp/<slug>/chats/session-*.jsonl` | JSONL (legacy `.json`) | per-project dirs under one root | yes (`~/.gemini/projects.json` + `.project_root` marker + `directories` field) | tokens per message, no cost | Medium |
| OpenCode | yes | `~/.local/share/opencode/opencode.db` | SQLite (drizzle) | global DB, sessions carry `directory` | yes (`session.directory`) | cost + 5 token counters on the session row | Medium (needs sqlite3 + JSON columns) |
| Crush | yes | `<project>/.crush/crush.db` (default) | SQLite, one DB per project | per-project; global registry `projects.json` lists them | yes (registry `path`) | cost + prompt/completion tokens on the session row | Medium |
| Codex CLI (related) | yes | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | JSONL | global, date-sharded | yes (in `session_meta`, from memory, unverified here) | tokens in events | Easy-Medium |
| Copilot CLI (related) | yes | `~/.copilot/session-state/<uuid>/` | `workspace.yaml` + `events.jsonl` | global | yes (workspace.yaml) | not verified | Medium |
| Goose (related) | yes | `~/.local/share/goose/sessions/sessions.db` | SQLite | global | yes | tokens | Medium |

---

## 1. Qwen Code (QwenLM/qwen-code)

Closest to Claude Code. It is a fork of Gemini CLI that adopted a Claude-style JSONL transcript.

**Persists and resumes:** yes.

**Storage (Linux):** `~/.qwen/projects/<sanitizeCwd(projectRoot)>/chats/<sessionId>.jsonl`.
- Base is `$QWEN_HOME` if set, else `~/.qwen`: `packages/core/src/config/storage.ts` (`getProjectDir()` joins `runtimeBaseDir`, `projects`, `sanitizeCwd(root)`; the `chats` segment is added at the runtime-status path near line 736). https://github.com/QwenLM/qwen-code/blob/main/packages/core/src/config/storage.ts
- `sanitizeCwd` replaces every non-alphanumeric char with `-` (`cwd.replace(/[^a-zA-Z0-9]/g, '-')`): `packages/core/src/utils/paths.ts`. This is lossy, same as Claude's encoding, so use the in-record `cwd` instead of decoding the directory name.
- Caveat: `ChatRecordingService.ts` header comment (as returned by a doc fetch) says `~/.qwen/tmp/<project_id>/chats/`. `storage.ts` is the current code and says `projects/`. Older versions used `tmp/`. A scanner should check both roots.
- There is also a sibling `<sessionId>.runtime.json` in `chats/` (storage.ts ~line 730) that external observers can read for live state.

**Format:** JSONL, one record per line, a `parentUuid` tree (the file is append-only, resume rebuilds the linear chain). Source: https://github.com/QwenLM/qwen-code/blob/main/packages/core/src/services/chatRecordingService.ts
Fields per record: `uuid`, `parentUuid`, `sessionId`, `timestamp` (ISO 8601), `type` (`user`|`assistant`|`tool_result`|`system`), `cwd`, `version`, `gitBranch`, `message` (Gemini-style Content with parts), `usageMetadata`, `model` (assistant records), optional `subtype`, `systemPayload`, `toolCallResult`.

**How to read each field:**
- id: `sessionId` / filename stem.
- title: a `system` record with `subtype: "custom_title"` and `systemPayload.customTitle` (+ `titleSource: "auto"|"manual"`). Last one wins, same as Claude. Design doc: https://github.com/QwenLM/qwen-code/blob/main/docs/design/session-title/session-title-design.md . If no title, fall back to the first user prompt.
- cwd: `cwd` on any record.
- timestamps: first and last record `timestamp` (or file mtime).
- model: `model` on assistant records.
- first/last prompt: first/last `type:"user"` record, text parts of `message.parts`.
- last assistant message: last `type:"assistant"` record, text parts.
- tokens: sum `usageMetadata` (Gemini-style `promptTokenCount` / `candidatesTokenCount` etc., field names inferred from the Gemini lineage, not read directly). No dollar cost is recorded; would need a price table.
- Compression/slash-command records have `subtype` and should be skipped when finding prompts.

**Resume:** flags defined in `packages/cli/src/config/top-level-options.ts`:
- `qwen --continue` / `-c`: "Resume the most recent session for the current project."
- `qwen --resume <id>` / `-r <id>`: "Resume a specific session by its ID. Use without an ID to show session picker."
- `--session-id <id>` is for starting with a caller-chosen id (mutually exclusive with continue/resume); `--fork-session` requires continue/resume (config.ts lines ~806-818).
- Resume is cwd-scoped, so chdir to the recorded `cwd` first, exactly as claude-sessions already does.
- Auto-approve: `--yolo` / `-y` ("Automatically accept all actions"), plus `--approval-mode` (same option set as Gemini).
- Other: `qwen sessions ps` lists live sessions (docs/users/features/commands.md); not useful for history.

**Per-project or global:** per-project directories under a single global root, so listing every project is a single glob.

**Feasibility: Easy.** Same shape as Claude Code: JSONL, `cwd`, custom title records, `--resume <id>`, `--yolo`. A parser variant of the existing one is nearly enough.

---

## 2. Gemini CLI (google-gemini/gemini-cli)

**Persists and resumes:** yes.

**Storage (Linux):** `~/.gemini/tmp/<project-identifier>/chats/session-<YYYY-MM-DDTHH-MM>-<first8ofSessionId>.jsonl`.
- Docs: "Sessions are stored in `~/.gemini/tmp/<project_hash>/chats/`" https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/session-management.md
- Source (newer than the doc): the identifier is a short slug from a project registry, not a SHA-256 any more. `packages/core/src/config/storage.ts` calls `registry.getShortId(projectRoot)`; the registry is `~/.gemini/projects.json`, and each `tmp/<slug>/` (and `history/<slug>/`) holds a `.project_root` marker file naming the owner (`projectRegistry.ts`, `PROJECT_ROOT_FILE = '.project_root'`). Legacy installs have SHA-256-of-path directory names (`createHash('sha256').update(path)`), migrated to slugs by `StorageMigration`. A scanner has to handle both.
- File naming and records: https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/chatRecordingService.ts . Subagent sessions are nested under a parent directory as `<sessionId>.jsonl` and have `kind: "subagent"`; skip them.

**Format:** JSONL (legacy `.json` is auto-migrated). Records: initial metadata, message records, update records of the form `{"$set": {...}}`, patch records, rewind records. So a reader must apply `$set` updates and rewinds, not just concatenate lines (this is the main added complexity versus Claude).
- Session metadata (ConversationRecord): `sessionId`, `projectHash`, `startTime`, `lastUpdated`, optional `summary`, `kind` (`main`|`subagent`), `directories` (workspace context), `memoryScratchpad`.
- Message (MessageRecord): `id`, `timestamp`, `type` (`user`|`gemini`), `content`, `displayContent`, `model` (gemini messages), `thoughts`, `tokens` (input/output/cached/thoughts/tool/total), `toolCalls`.

**How to read each field:** id = `sessionId`; title = `summary` (auto-generated, may be absent; otherwise first user message); cwd = `directories[0]` or reverse-map the directory slug via `~/.gemini/projects.json`/`.project_root` (the legacy hash cannot be reversed, only matched by hashing candidate paths); timestamps = `startTime`/`lastUpdated`; model = `model` on `gemini` messages; first/last prompt = first/last `type:"user"` `content`; last assistant message = last `type:"gemini"` `content`; tokens = sum of `tokens.*`. No cost field.

**Resume** (https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/cli-reference.md and session-management.md):
- `gemini --resume` / `-r` (latest), `gemini --resume latest`, `gemini --resume <index>`, `gemini --resume <uuid>`; `gemini --list-sessions` for the current project (date, message count, id). In-app `/resume` opens a session browser.
- Index is only meaningful relative to the current project's list, so use the UUID.
- Auto-approve: `--approval-mode yolo` (choices `default|auto_edit|yolo|plan`); `--yolo` is deprecated but still accepted. `-y` is not documented.
- Retention: default 30 days (`maxAge`, `maxCount`, `minRetention` in `settings.json`), so old sessions disappear.

**Per-project or global:** per-project (docs: "Sessions are project-specific"). Resume must be run from the project's cwd; if the cwd is wrong, the session will not be found.

**Feasibility: Medium.** Resume by UUID plus cwd is simple, but the two directory-naming schemes (slug vs legacy hash), `$set`/rewind records and the doc/source mismatch need care. cwd recovery is reliable only for registered slugs. I did not read `directories` population code; verify on a real machine before relying on it.

---

## 3. OpenCode (sst/opencode, now github.com/anomalyco/opencode)

`sst/opencode` redirects to `anomalyco/opencode`; default branch `dev`.

**Persists and resumes:** yes.

**Storage (Linux):** SQLite at `~/.local/share/opencode/opencode.db`.
- Path function: `packages/core/src/database/database.ts` `path()`: `Flag.OPENCODE_DB` override, else `join(Global.Path.data, "opencode.db")` for the main release channel, else `opencode-<channel>.db` for other channels. Data dir is `~/.local/share/opencode` on Linux/macOS (https://opencode.ai/docs/troubleshooting/). `opencode db path` prints the live path.
- Older versions kept JSON files at `~/.local/share/opencode/storage/session/<projectID>/<sessionID>.json` (`packages/opencode/src/storage/storage.ts`, which still migrates them). The troubleshooting doc still describes the old `project/` layout and does not mention the DB, so docs are stale. A scanner should read the DB and optionally fall back to the legacy JSON tree.

**Schema** (`packages/core/src/session/sql.ts`, `project/sql.ts`): https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/sql.ts
- `session`: `id`, `project_id`, `workspace_id`, `parent_id` (non-null for subagent/child sessions), `slug`, `directory` (the cwd), `path`, `title`, `version`, `summary_additions`/`summary_deletions`/`summary_files` (lines changed), `cost` (real), `tokens_input`, `tokens_output`, `tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write`, `agent`, `model` (JSON `{id, providerID, variant}`), `time_created`, `time_updated`, `time_compacting`, `time_archived`. Timestamps are epoch milliseconds (`Date.now()` defaults).
- `message`: `id`, `session_id`, `time_created`, `data` (JSON; includes `role`, and model info for assistant messages per the v1 session type).
- `part`: `id`, `message_id`, `session_id`, `data` (JSON; text parts have `type:"text"` and `text`; I did not read the part type definitions, verify the exact keys).
- `project`: `id`, `worktree`, `vcs`, `name`. Newer tables `session_message` / `session_input` (v2 event-style records) also exist; which one holds the transcript for current builds is not confirmed.

**How to read each field:** id/title/cwd/model/cost/tokens/lines changed are columns on `session`, which makes the list view cheap and richer than Claude's (cost is exact, no estimation). First prompt: first `message` where `data.role='user'` joined to its text `part`. Last assistant message: last assistant `message`'s text parts. Hide `parent_id IS NOT NULL` and `time_archived IS NOT NULL` by default.

**Resume** (https://opencode.ai/docs/cli/ and `packages/opencode/src/cli/cmd/tui.ts`):
- `opencode --session <id>` / `-s <id>`; `opencode --continue` / `-c` (last session); `--fork` (needs `-c` or `-s`); optional project path positional argument.
- Auto-approve: `--auto` ("auto-approve permissions that are not explicitly denied (dangerous!)"); `tui.ts` also accepts `--yolo` and `--dangerously-skip-permissions` as aliases (all feed the same `auto` flag). The `run` subcommand also has `--auto`.
- Helpers: `opencode session list [-n N] [--format json]`, `opencode export [id]`, `opencode import`, `opencode stats`, `opencode db <query>` / `opencode db path`.

**Per-project or global:** global DB; the session is scoped to a project (`project_id`, `directory`). `--continue` is evaluated for the cwd's project, so chdir to `session.directory` first. Whether `-s <id>` works from any directory is not confirmed from source; chdir anyway.

**Feasibility: Medium.** No transcript parsing at all for the list, only SQL, and cost/tokens are exact. Costs: Python's `sqlite3` is enough (stdlib), but the schema is moving fast (v1 `message`/`part` plus new v2 tables, channel-suffixed DB names), so query defensively and read-only (`file:...?mode=ro`). WAL mode means a running opencode can hold the DB, read-only open still works.

---

## 4. Crush (charmbracelet/crush)

**Persists and resumes:** yes.

**Storage:** SQLite `crush.db` inside the project's data dir.
- `internal/config/config.go`: `defaultDataDirectory = ".crush"`, "Relative paths are resolved against the working directory", so the default DB is `<project>/.crush/crush.db` (`internal/db/connect.go`: `filepath.Join(dataDir, "crush.db")`). Overridable with `--data-dir/-D` or the `data_directory` config key.
- The README says global state lives at `$HOME/.local/share/crush/crush.json` (Linux) and `%LOCALAPPDATA%\crush\crush.json` (Windows); that file is global state, not sessions.
- Enumeration across projects: `internal/projects/projects.go` keeps `~/.local/share/crush/projects.json` (the parent dir of `GlobalConfigData()`) with `[{path, data_dir, last_accessed}]`, updated by `Register(workingDir, dataDir)` each time Crush runs in a directory. That gives the cwd and the DB location for every known project, and `crush projects` exposes it (`internal/cmd/projects.go`). Projects used before this registry existed will not be listed.

**Schema** (`internal/db/migrations/*.sql`, `internal/db/models.go`): https://github.com/charmbracelet/crush/blob/main/internal/db/migrations/20250424200609_initial.sql
- `sessions`: `id`, `parent_session_id`, `title`, `message_count`, `prompt_tokens`, `completion_tokens`, `cost` (float), `created_at`, `updated_at` (Unix seconds), `summary_message_id`, `todos`, `channel`. No model column and no cwd column (cwd comes from the registry entry / DB location).
- `messages`: `id`, `session_id`, `role`, `parts` (JSON array of typed parts: text, reasoning, tool calls/results, binary), `model`, `provider`, `created_at`, `updated_at`, `finished_at`, `is_summary_message`.
- `files` / `read_files`: file-version tracking, not needed.
- CLI: `crush session list|show <id>|last|delete|rename --json` (`internal/cmd/session.go`), usable instead of raw SQL, but it works per data dir (use `-D`/`-c`).

**How to read each field:** title/cost/tokens/timestamps from `sessions`; model from the last assistant `messages.model`; first/last prompt and last assistant message from `messages.parts` (JSON; text part shape in `internal/message/content.go`, not decoded in detail here). Skip `parent_session_id IS NOT NULL` (sub-agent/tool sessions).

**Resume** (`internal/cmd/root.go`):
- `crush --session <id>` / `-s <id>` ("Continue a previous session by ID"); `crush --continue` / `-C` ("Continue the most recent session"); the two are mutually exclusive. `-c` is `--cwd`, not continue. Session IDs can be prefixes/hashes (`resolveWorkspaceSessionID`).
- Auto-approve: `--yolo` / `-y` ("Automatically accept all permissions (dangerous mode)").
- Must run in (or `-c`/`--cwd` to) the project directory so the right `.crush` is found. The exit banner prints a `crush -s <id>` hint.

**Per-project or global:** per-project DBs; global only through the `projects.json` registry.

**Feasibility: Medium.** Data is clean and has exact cost, but it is N databases discovered through a registry file that only covers projects run on a recent Crush; deleted or moved project dirs leave stale entries; a project with a custom data dir needs the `data_dir` from the registry. No model on the session row.

---

## 5. Closely related CLIs found (shallow, web/doc evidence only, not source-verified)

- **OpenAI Codex CLI**: sessions at `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`; `codex resume` (picker), `codex resume --last`, `codex resume <id-or-name>`, `--all` to include other projects; `/rename` names are stored in `~/.codex/session_index.jsonl`. Sources: https://github.com/openai/codex/issues/20165 , https://inventivehq.com/knowledge-base/openai/how-to-resume-sessions . Likely the next-best target after Qwen Code; JSONL, global, date-sharded. Large rollouts can be huge (https://github.com/openai/codex/issues/30932), so tail-read like the existing code does. Auto-approve flag not checked here.
- **GitHub Copilot CLI**: `~/.copilot/session-state/<uuid>/` with `workspace.yaml` (metadata) and `events.jsonl`; `copilot --continue`, `copilot --resume` (picker), `copilot --resume=<id>`. Sources: https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/session-persistence , https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/work-with-multiple-sessions . Metadata is in a small YAML file, so listing is cheap.
- **Goose (Block)**: SQLite at `~/.local/share/goose/sessions/sessions.db` since v1.10; `goose session --resume --name <name>` (also `-i` / `--session-id` in newer versions, unverified). Source: https://ccusage.com/guide/goose/ , https://goose-docs.ai/docs/guides/logs/ .
- **Aider** (not searched): known to write `.aider.chat.history.md` in the project dir and has `--restore-chat-history`; this is a per-project Markdown log with no session ids, so it is a poor fit (from memory, unverified).
- **Kilo / Cline / Roo**: the CLI variants were not investigated.

## Cross-tool implications for claude-sessions

- Ranked by effort: Qwen Code (Easy, near-identical parser) > Codex (JSONL) > Gemini (JSONL with `$set`) > OpenCode / Crush / Goose (SQLite via stdlib `sqlite3`, read-only).
- A small adapter interface is enough: `discover() -> [Session]` and `resume_argv(session, yolo) -> (cwd, argv)`. Resume argv per tool: `claude --resume <id>`, `qwen --resume <id>`, `gemini --resume <uuid>`, `opencode -s <id>`, `crush -s <id>`; yolo: `--dangerously-skip-permissions`, `--yolo`, `--approval-mode yolo`, `--auto`, `--yolo`.
- Everything except OpenCode resumes by cwd, so keep the existing "chdir to the session's cwd, then exec" step.
- Only OpenCode and Crush record exact cost; Qwen and Gemini record tokens only, so cost there would be an estimate, or just omitted.
- Not verified anywhere in this report: real on-disk samples, exact `usageMetadata` key names for Qwen, `directories` population for Gemini, OpenCode part JSON keys, Crush `parts` JSON shape. Each adapter's first task should be to inspect one real file.

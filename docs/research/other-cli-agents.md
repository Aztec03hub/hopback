# Other CLI/TUI coding agents: session and resume support

Date: 2026-10-01. Scope: CLI/TUI agents not covered by other sweeps. Evidence: official docs where they exist, otherwise third-party reverse-engineering (marked "3P"). Nothing was installed or run, so every path and schema below is from docs or third-party reports and has NOT been checked on a real machine.

Feasibility key: Easy = plain JSON/JSONL, cwd and title recoverable, simple resume command. Medium = usable but needs SQLite, lossy cwd, or version-split layouts. Hard = opaque/protobuf/server-side. Not possible = no local data.

## Findings

### 1. Mistral Vibe (`vibe`) - Easy
- Resume: `vibe --continue`/`-c` (latest), `vibe --resume [ID]` (picker or partial id); in-session `/resume`. Sessions are scoped per directory. https://github.com/mistralai/mistral-vibe
- Storage: `$VIBE_HOME/logs/session/` else `~/.vibe/logs/session/`. One directory per session: `meta.json` (metadata, token totals, cost in `stats.session_cost`, timestamps, working directory, model) and `messages.jsonl` (non-system messages and tool calls). Subagents nest under `agents/`. https://github.com/getagentseal/codeburn/blob/main/docs/providers/mistral-vibe.md (3P)
- Caveat: Vibe ~2.25.5 ("Unified Harness", late Sep 2026) writes newer sessions to `logs/session/unified/` with a different structure. https://github.com/getagentseal/codeburn/issues/1596 (3P). Must handle both layouts. `save_dir` in `[session_logging]` can relocate the store.
- Exact `meta.json` key names for cwd/title/id not confirmed; inspect a real file first.

### 2. Grok Build (xAI `grok`) - Easy
- Resume: `grok -c` (latest in cwd), `grok -r [ID|title]`, `/resume` picker. https://docs.x.ai/build/cli/reference
- Storage: `~/.grok/sessions/<encoded-cwd>/<session-id>/` (`$GROK_HOME` override). The cwd is URL-encoded; if over 255 bytes it becomes slug+hash with the original path in a `.cwd` file. `summary.json` = title, generated title, model, timestamps, message counts, parent id. `updates.jsonl` is the authoritative log; `chat_history.jsonl` also present. https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/17-sessions.md (official)
- Note: community `superagent-ai/grok-cli` is a different tool; not assessed.

### 3. Kimi Code CLI (`kimi`) - Easy (path needs verification)
- Replaces the archived `MoonshotAI/kimi-cli` (read-only, stops working). https://github.com/MoonshotAI/kimi-cli
- Resume: `kimi --continue`/`-C` (latest in cwd), `kimi --session <id>`/`-S`, `--session` alone = picker; `/sessions`, `/resume`, `/fork`. https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/guides/sessions.md
- Storage: `$KIMI_CODE_HOME/sessions/` default `~/.kimi-code/sessions/`, grouped by a `workDirKey`; per session `state.json` (title, created time), `agents/*/wire.jsonl` (event stream); legacy `~/.kimi/sessions/` has `context.jsonl`, `wire.jsonl`, `state.json`. https://www.kimi-cli.com/en/configuration/data-locations.html (3P/legacy docs).
- Caveat: `workDirKey` is a hash (legacy docs say working-directory hash), so cwd may not be recoverable from the path. Check whether `state.json` or `wire.jsonl` records it. Legacy `~/.kimi/` is migrated (copied) by the new CLI, so scan both.

### 4. Factory Droid (`droid`) - Easy/Medium
- Resume: `droid --resume [sessionId]` (default last modified); `droid exec -s <id>`. Sessions are directory-scoped since CLI 0.26.7. https://docs.factory.ai/reference/cli-reference ; 3P summary https://deepwiki.com/factory-ai/factory/4.5-session-management
- Storage (3P, not in official docs I could fetch): `~/.factory/sessions/{uuid}/` with `conversation.jsonl`, `settings.json`, `files/`. How cwd is recorded (path encoding vs field) is unconfirmed. Official docs page I fetched did not cover storage.

### 5. Kiro CLI (`kiro-cli`; successor of Amazon Q Developer CLI) - Easy (new) / Medium (old)
- Resume: `kiro-cli chat --resume` (latest in cwd), `--resume-picker`, `--resume-id <UUID>`; `/chat resume`. Only one process may hold a session. https://kiro.dev/docs/cli/chat/session-management/ (official)
- New layout (3P): `~/.kiro/sessions/cli/<id>.json` (id, cwd, created_at, updated_at, state), `<id>.jsonl` (role/content/timestamp/tool_uses), `<id>.lock` (active marker). https://github.com/vshulcz/deja-vu/issues/3103
- Old layout: SQLite `~/.local/share/kiro-cli/data.sqlite3` (macOS `~/Library/Application Support/kiro-cli/`), table `conversations_v2`, keyed by working directory; Amazon Q Dev CLI used `~/.local/share/amazon-q/data.sqlite3` with a `conversations` table. https://github.com/mxmehl/kiro-cli-history (existing TUI that already resumes these)
- Quirk: a `--list-sessions` bug from `$HOME` is reported (https://github.com/kirodotdev/Kiro/issues/6440). A `.lock` file can mean "active elsewhere".

### 6. Cursor CLI (`cursor-agent` / `agent`) - Medium
- Resume: `agent ls` (picker), `agent resume` (latest), `agent --continue`, `agent --resume="<chat-id>"`. https://cursor.com/docs/cli/overview (official; storage not documented)
- Storage (3P): `~/.cursor/chats/<workspace-hash>/<chat-uuid>/store.db` (SQLite: `meta`, content-addressed protobuf `blobs`) plus `meta.json` with `schemaVersion`, `createdAtMs`, `updatedAtMs`, `cwd`, `hasConversation`. Meta in `store.db` holds `agentId`, `name` (title), `lastUsedModel`. https://github.com/vshulcz/deja-vu/issues/3772 , https://github.com/getagentseal/codeburn/issues/986
- Legacy/also-written JSONL: `~/.cursor/projects/<encoded-cwd>/agent-transcripts/<id>/<id>.jsonl`.
- Feasibility: listing, title, cwd, last-active are easy from `meta.json` + `store.db` meta. Message previews need either the JSONL transcript or protobuf decoding (hard). Resume id = chat UUID / `agentId`; confirm they match.
- Does not share storage with the Cursor IDE (`state.vscdb`), which is out of scope.

### 7. Pi coding agent (`pi`, badlogic/pi-mono, now earendil-works/pi) - Easy
- Resume: `pi --resume` / `/resume` picker (also `-c` continue per upstream docs; not confirmed in what I fetched). https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/usage.md
- Storage: `~/.pi/agent/sessions/--<path>--/<timestamp>_<session-id>.jsonl`; path encoding replaces `/`, `\`, `:` with `-` (lossy) but the first-line SessionHeader carries id, timestamp and cwd. Entries form a tree via `id`/`parentId`; `SessionInfoEntry` carries the `/name` title. https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/session-format.md (official). Structurally the closest match to Claude Code JSONL.
- Not in the candidate list but found; worth adding.

### 8. OpenHands CLI (`openhands`) - Easy
- Resume: `openhands --resume` (list), `--resume <conversation-id>`, `--resume --last`. https://docs.openhands.dev/openhands/usage/cli/resume
- Storage: `~/.openhands/conversations/<id>/conversation.json` (official). Unknown: whether cwd is stored (likely in the SDK conversation state; check a real file), and whether larger conversations use multiple event files. Likely Easy once schema is inspected.

### 9. Continue CLI (`cn`) - Easy
- Resume: `cn --resume` (last session in this terminal), `cn ls` / `cn ls --json`, `cn --fork <id>`. https://www.npmjs.com/package/@continuedev/cli
- Storage (3P): `~/.continue/sessions/<sessionId>.json` plus an index `~/.continue/sessions/sessions.json` listing title, date, `workspaceDirectory`; `CONTINUE_GLOBAL_DIR` overrides. https://github.com/vshulcz/deja-vu/issues/3062 , https://github.com/vshulcz/deja-vu/issues/4375 (measured against cn 1.5.47). The sessions dir is shared with the Continue IDE extension, so IDE sessions will also appear (may not resume in `cn`).
- Shortcut: `cn ls --json` is a supported listing path and avoids parsing.

### 10. Open Interpreter (new `interpreter` CLI) - Medium
- Resume: `interpreter resume --last`, `interpreter resume` (picker), `--all` (all directories), `interpreter resume <SESSION_ID>`. https://www.openinterpreter.com/docs/terminal/sessions
- Storage: "stored in `~/.openinterpreter/`"; exact layout/format not documented in what I fetched. The classic Python project used JSON files under a conversations dir (https://github.com/endolith/open-interpreter, community fork, active Sep 2026). Two different generations exist, so detect which.

### 11. Auggie (Augment) - Hard
- Resume: `auggie --continue`/`-c`, `auggie --resume [id|prefix]`, `auggie session list [--json]`, `auggie session resume`. https://docs.augmentcode.com/cli/reference
- Storage: `~/.augment/sessions` is mentioned in official docs, but I found no format spec. `~/.augment/session.json` is the OAuth token file (do not read it). A DeepWiki page says session history is held via the Augment backend, with only workspace metadata local. https://deepwiki.com/augmentcode/auggie/3.3-session-management (3P, uncertain).
- Practical route: shell out to `auggie session list --json`. Feasible only if that output includes workspace and timestamps; unverified.

### 12. Google Antigravity CLI (`agy`; replaces Gemini CLI per a 3P report) - Hard
- Resume: `agy -c`, `agy --conversation <id>`, `/resume` picker. Cache `~/.gemini/antigravity-cli/cache/last_conversations.json` maps workspace path to latest conversation id. https://antigravity.google/docs/cli/commands/resume/ (official)
- Storage (3P): `~/.gemini/antigravity-cli/conversations/<uuid>.db` (SQLite with protobuf blobs, no cwd column); `conversation_summaries.db` holds working-directory URIs and titles. https://github.com/garrytan/gstack/issues/1977 , https://agentgrep.org/backends/antigravity-cli/
- Feasibility: list + title + cwd + resume are feasible via `conversation_summaries.db` (Medium for that subset); message previews are not (protobuf, private schema). Gemini CLI itself is covered elsewhere.

### 13. Rovo Dev CLI (Atlassian, `acli rovodev`) - Easy/Medium
- Resume: `acli rovodev run --restore` (last session); session management docs https://support.atlassian.com/rovo/docs/manage-sessions-in-rovo-dev-cli/
- Storage: `~/.rovodev/sessions/` with `session_context.json` and `metadata.json` per session (search snippet of Atlassian support docs; I did not open the page). Resume-by-id and cwd field not verified.

### 14. Snowflake Cortex Code (`cortex` / CoCo) - Medium
- Resume: `cortex --continue`, `cortex --resume <session_id>`. https://docs.snowflake.com/en/user-guide/cortex-code/cli-reference
- Storage: `~/.snowflake/cortex/conversations/` (from search snippet). Format and cwd unverified. Niche (Snowflake-bound), low priority.

### 15. Devin CLI (`devin`) - Hard (unknown)
- Resume: `devin -c`, `devin -r` (picker). https://docs.devin.ai/cli/essential-commands
- Storage: not found. Sessions may sync to the cloud. Cannot be assessed without installing.

### 16. JetBrains Junie CLI - Hard (unknown)
- Resume: `--resume` for the last session; `-c/--cache-dir` option. https://junie.jetbrains.com/docs/parameters.html
- Storage format not found. Needs inspection on a real install.

### 17. Warp Agent CLI (`warp`/`oz`) - Hard
- Resume: `warp --resume <CONVERSATION_TOKEN>` (printed on exit), `/conversations` menu. Conversations are stored locally unless cloud-sync is enabled. https://docs.warp.dev/agents/cli/agent-conversations/
- Local format/path not documented; Warp's desktop app keeps state in its own SQLite store (not verified for the CLI). Token is not a plain session id. Resume needs the Warp binary context. Treat as Hard until a real install is inspected.

### 18. Letta Code (`letta`) - Hard
- Cloud backend by default; agents and conversations live server-side. Local mode: `letta --backend local`, state in `~/.letta/lc-local-backend` (`LETTA_LOCAL_BACKEND_DIR`), agent memory as git repo under `memfs/<agent-id>/memory`. https://docs.letta.com/letta-agent/local-mode , https://docs.letta.com/letta-agent/cli-reference
- Model is agent-centric (one persistent agent, many conversations) rather than session-per-directory; resume flags not confirmed. Poor fit.

### 19. simonw `llm` - Medium (limited fit)
- Chat tool, not a coding agent with file-editing sessions. `llm -c` continues the last conversation, `--cid <id>` a specific one. Logs in SQLite `log.db` (path from `llm logs path`); v0.32 changed to a new content-addressed schema. https://llm.datasette.io/en/stable/usage.html
- No cwd stored, so "chdir to the session's cwd" has no meaning. Include only if non-coding chats are wanted.

### 20. Plandex - Hard
- Plans live in a server (Docker/Postgres); Plandex Cloud shut down Nov 2025; self-hosted survives (v2.2.1). https://dev.to/jovan_chan_9500711396d4e6/plandex-v2-review-2026-cloud-shut-down-self-hosted-survives-is-the-terminal-agent-still-worth-11pp (3P). Plan state is not file-based locally; not a fit.

## Excluded / not assessed

| Tool | Reason |
|---|---|
| Kimi CLI (`MoonshotAI/kimi-cli`) | Archived, replaced by Kimi Code CLI (above); only its legacy `~/.kimi` data matters |
| Open Interpreter classic (OpenInterpreter/open-interpreter) | Superseded by the new CLI above; only community forks continue the Python version |
| Plandex Cloud | Shut down Nov 2025 |
| `aichat`, `mods` | Not researched in this pass (my search for them returned nothing usable); both are chat tools with no cwd concept. Recommend skipping unless wanted |
| Roo Code and other VS Code forks | IDE extensions, out of scope (Cline/Kilo covered elsewhere) |
| Cursor IDE, Claude Desktop, Antigravity 2.0 desktop | GUI, out of scope |
| `superagent-ai/grok-cli` | Community project; xAI's official Grok Build supersedes it for this purpose |

## Summary table

| Tool | Binary | Sessions | Resume | Local storage | Format | cwd recoverable | Feasibility |
|---|---|---|---|---|---|---|---|
| Mistral Vibe | `vibe` | yes | `vibe --resume <id>` | `~/.vibe/logs/session/` (+`unified/`) | dir: meta.json + messages.jsonl | yes (meta) | Easy |
| Grok Build | `grok` | yes | `grok --resume <id>` | `~/.grok/sessions/<enc-cwd>/<id>/` | summary.json + jsonl | yes (path/.cwd) | Easy |
| Pi | `pi` | yes | `pi --resume` | `~/.pi/agent/sessions/--path--/*.jsonl` | JSONL tree | yes (header) | Easy |
| Kimi Code | `kimi` | yes | `kimi --session <id>` | `~/.kimi-code/sessions/` | state.json + wire.jsonl | unclear (hashed dir) | Easy (verify cwd) |
| OpenHands | `openhands` | yes | `openhands --resume <id>` | `~/.openhands/conversations/<id>/` | conversation.json | unverified | Easy |
| Continue | `cn` | yes | `cn --fork/--resume`, `cn ls --json` | `~/.continue/sessions/` | JSON + index | yes (workspaceDirectory) | Easy |
| Kiro CLI | `kiro-cli` | yes | `kiro-cli chat --resume-id <id>` | `~/.kiro/sessions/cli/`; old SQLite | json+jsonl; SQLite | yes | Easy (new) / Medium (old) |
| Factory Droid | `droid` | yes | `droid --resume <id>` | `~/.factory/sessions/<id>/` | conversation.jsonl | unverified | Easy/Medium |
| Rovo Dev | `acli rovodev` | yes | `run --restore` | `~/.rovodev/sessions/` | JSON | unverified | Easy/Medium |
| Cursor CLI | `agent` | yes | `agent --resume=<id>` | `~/.cursor/chats/<hash>/<id>/` | meta.json + SQLite/protobuf | yes (meta.json) | Medium |
| Open Interpreter (new) | `interpreter` | yes | `interpreter resume <id>` | `~/.openinterpreter/` | undocumented | unknown | Medium |
| Cortex Code | `cortex` | yes | `cortex --resume <id>` | `~/.snowflake/cortex/conversations/` | unknown | unknown | Medium |
| llm (simonw) | `llm` | chat only | `llm -c` / `--cid` | SQLite log.db | SQLite | no | Medium (poor fit) |
| Antigravity CLI | `agy` | yes | `agy --conversation <id>` | `~/.gemini/antigravity-cli/conversations/*.db` | SQLite+protobuf | via summaries db | Hard |
| Auggie | `auggie` | yes (backend-held?) | `auggie --resume <id>` | `~/.augment/sessions` (undocumented) | unknown | unknown | Hard |
| Warp CLI | `warp` | yes | `warp --resume <token>` | undocumented | unknown | unknown | Hard |
| Junie CLI | `junie` | yes | `--resume` | undocumented | unknown | unknown | Hard |
| Devin CLI | `devin` | yes | `devin -r` / `-c` | not found | unknown | unknown | Hard |
| Letta Code | `letta` | server-side agents | agent-based | `~/.letta/lc-local-backend` (local mode) | git memfs + backend | no | Hard |
| Plandex | `plandex` | server plans | n/a | Docker/Postgres | DB | no | Hard / skip |

## Suggested order for implementation

1. Mistral Vibe, Grok Build, Pi, Continue, Kiro (new layout): documented or well-reported JSON/JSONL with cwd.
2. Kimi Code, OpenHands, Droid: verify cwd storage on a real file first.
3. Cursor CLI: meta.json listing now; defer message previews.
4. Everything else only after inspecting a real install, since the formats are unverified.

All 3P claims should be re-checked against a real install before coding against them; schemas for several tools changed within the last two months (Vibe unified harness, Kiro SQLite to files, Cursor store.db, Kimi rename).

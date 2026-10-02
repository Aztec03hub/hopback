# Other CLI agents: session storage and resume feasibility

Date: 2026-10-01. Scope: Aider, Goose, Amp CLI, GitHub Copilot CLI, Cline CLI, Kilo Code CLI, plus related CLIs.
Method: official docs fetched, plus third-party parsers that read each store (cited because they show real on-disk shapes). Nothing installed or run. "UNVERIFIED" = not confirmed from a source this session; check against a real install before coding.

## Summary table

| CLI | Persists + resumes | Store | Format | Scope | Rating |
|---|---|---|---|---|---|
| Copilot CLI | yes | `~/.copilot/session-state/<uuid>/` | events.jsonl + workspace.yaml (+ session-store.db) | global, cwd inside | Easy |
| Goose | yes | `~/.local/share/goose/sessions/sessions.db` | SQLite | global, working_dir column | Easy |
| Kilo CLI | yes | `~/.local/share/kilo/kilo.db` | SQLite (OpenCode schema) | global, directory column | Easy-Medium |
| OpenCode (related) | yes | `~/.local/share/opencode/opencode.db` | SQLite | global | Easy-Medium |
| Cline CLI | yes | `~/.cline/data/sessions/<id>/` | manifest JSON + messages JSON | global, cwd in manifest | Medium |
| Amp CLI | yes, but server-backed | `~/.local/share/amp/threads/<id>.json` | one JSON per thread | global; cloud-synced | Medium |
| Aider | no real session concept | `.aider.chat.history.md` in each project | markdown append log | per-project, no registry | Hard |

## Copilot CLI (`copilot`)

- Persistence/resume: yes. `copilot --resume` (picker) / `/resume`, `copilot --continue` (most recent). Docs: https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-copilot-cli and https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference . Picker sorts by relevance/last used/created/name. `--resume=<id>` form: UNVERIFIED in docs fetched (parameter form seen in third-party tools, e.g. https://jonmagic.com/posts/github-copilot-session-search-and-resume-cli/ ; read before relying).
- Location: `~/.copilot` (override `COPILOT_HOME`; `--config-dir` deprecated).
- Layout (https://deepwiki.com/github/copilot-cli/6.2-session-state-and-lifecycle-management, https://pkg.go.dev/github.com/neilberkman/ccrider/pkg/copilotsessions):
  `~/.copilot/session-state/<uuid>/{events.jsonl, workspace.yaml, plan.md, checkpoints/, files/}`; also `session-store.db` (SQLite index, reported to lose assistant text, so parse events.jsonl).
- Fields:
  - id: directory name (UUID); also `session.start.data.sessionId`.
  - title/name, cwd, git branch, timestamps: `workspace.yaml` (keys seen: id, cwd, summary, name; docs say title, branch, timestamps also).
  - cwd also in `session.start.data.context.cwd`; start time `data.startTime`; `copilotVersion`.
  - first/last prompt: `user.message` events, `data.content`; each line has `type, data, id, timestamp, parentId`.
  - last assistant message: `assistant.message` events.
  - model: `session.model_change` events (and compaction usage `model`).
  - tokens: per-event usage is sparse (compaction events carry `preCompactionTokens`, `compactionTokensUsed`); no cost. Schema source: https://gist.github.com/RockNoggin/8cc87f640ce5e3d284298a6db21f7523 . Schema is explicitly unstable.
- Auto-approve: `--allow-all` / `--yolo` (all tools, paths, URLs); finer: `--allow-tool`, `--allow-url`.
- Local: yes (conversation is local; model calls go to GitHub).
- Resume mechanics: needs to cd to cwd then `copilot --resume=<id>` (verify form); worst case `copilot --continue` only gets the latest.
- Rating: Easy. JSONL + YAML maps almost 1:1 to what we do for Claude; only risk is schema churn and verifying the resume-by-id flag.

## Goose (Block)

- Persistence/resume: yes. Docs: https://block.github.io/goose/docs/guides/sessions/session-management (raw: https://raw.githubusercontent.com/block/goose/main/documentation/docs/guides/sessions/session-management.md), CLI flags https://goose-docs.ai/docs/guides/goose-cli-commands/ .
- Location: `~/.local/share/goose/sessions/sessions.db` (SQLite; legacy `.jsonl` files from before v1.10.0 left on disk). Path from `Paths::data_dir()` + `sessions/sessions.db` (https://raw.githubusercontent.com/block/goose/main/crates/goose/src/session/session_manager.rs). Note the project moved orgs (github.com/aaif-goose/goose appears in search results); verify repo URL.
- Schema (session_manager.rs):
  - `sessions`: id, name, user_set_name, working_dir, created_at, updated_at, archived_at, message_count, last_message_timestamp, input/output/total/cache_read/cache_write tokens and `accumulated_*` versions, provider_name, model_config_json, goose_mode, session_type (User, Scheduled, SubAgent, Hidden, Terminal, Gateway, Acp), recipe_json, parent_session_id, project_id.
  - `messages`: message_id, session_id, role, content_json, created_timestamp, metadata_json. First/last prompt = first/last role='user'; last assistant = last role='assistant' (parse content_json).
  - `usage_ledger`: session_id, model, tokens, `cost`, cost_source, is_compaction. So cost is real, not estimated.
  - Filter `session_type = 'User'` to mimic our "hide agent sessions".
- id format `YYYYMMDD_<COUNT>`, e.g. `20251108_1`.
- Resume: `goose session --resume --session-id <id>` or `goose session -r --name <name>`; `-r` alone = most recent; `--fork` copies; `goose session list --format json -w <dir> -l N`. `goose run --resume`.
- Auto-approve: env `GOOSE_MODE` = `auto` | `approve` | `smart_approve` | `chat` (`/mode` in session). `auto` is the default if unset (https://github.com/aaif-goose/goose/issues/12448 ; permission modes at https://goose-docs.ai/docs/guides/managing-tools/goose-permissions/). So "yolo" = `GOOSE_MODE=auto goose session -r --session-id <id>`.
- Local, global store, working_dir per session.
- Rating: Easy. Open SQLite read-only (use `file:...?mode=ro`); schema is migrating, select columns defensively. Does goose resume cd to working_dir itself? UNVERIFIED; we cd anyway.

## Kilo Code CLI

- Fork of OpenCode (https://kilo.ai/docs/code-with-ai/platforms/cli). Persists and resumes.
- Location: `~/.local/share/kilo/kilo.db` (SQLite, OpenCode schema); `kilo db path` prints it (via https://kilo.ai/docs/code-with-ai/agents/session-history and issues https://github.com/Kilo-Org/kilocode/issues/14538 , https://github.com/vshulcz/deja-vu/issues/3643). Config in `~/.config/kilo/` (global) and `./.kilo/`. `kilo.db` can reach hundreds of MB.
- Schema (OpenCode; https://vibe-replay.com/blog/opencode-local-storage/): `session` (id, parent_id, slug, title, directory, time_created, time_updated, model JSON, cost, token cols vary by version), `message` (id, session_id, data JSON: role, time, model, tokens), `part` (id, message_id, session_id, data JSON: text/reasoning/tool/patch/...). Cost/tokens vary by version and are 0 for free models. Text for prompts/last assistant message is in `part.data` where type=text, joined to message role.
- Resume: `kilo --continue` / `-c` (most recent in this workspace); `--session <id>` form inherited from OpenCode (UNVERIFIED for Kilo); `/sessions` in TUI; `kilo session` subcommand (list subcommand not in docs fetched); `kilo export [id]` / `kilo import`.
- Auto-approve: `/auto-approve` toggles (saved to global config); `kilo run --auto`; permissions in `~/.config/kilo/kilo.jsonc` (allow/ask/deny). No `--dangerously-skip-permissions` documented. So "yolo" cannot be a pure flag on resume if only the toggle exists; use `--auto` for headless only (UNVERIFIED for TUI).
- Also imports legacy sessions from `~/.claude/projects/` and `~/.codex/sessions/` (so Kilo itself already reads Claude's store).
- Local; global with per-session `directory`.
- Rating: Easy-Medium. SQLite is simple, but JSON `data` columns and version drift need care; same adapter covers OpenCode.

## Cline CLI (v2+/3.x)

- Persists and resumes. Docs: https://docs.cline.bot/cli/cli-reference , https://docs.cline.bot/cline-cli/overview , https://cline.bot/blog/introducing-cline-cli-2-0 .
- Do not confuse with the VS Code extension store (`<vscode-globalStorage>/saoudrizwan.claude-dev/tasks/<id>/api_conversation_history.json`), which is a GUI and out of scope.
- CLI store (via third-party readers, not official docs: https://github.com/alondero/buildmesh/issues/1772 , https://github.com/junhoyeo/tokscale/issues/1369): `~/.cline/data/sessions/<id>/<id>.json` (manifest) and `<id>/<id>.messages.json`; index at `~/.cline/data/db/sessions.db` (no message content). Override: `CLINE_SESSION_DATA_DIR` > `CLINE_DATA_DIR` > `CLINE_DIR` > `~/.cline`; `--data-dir`.
- Manifest fields: session_id, source, pid, status, exit_code, interactive, provider, model, cwd, workspace_root, team_name, prompt (first prompt), metadata (usage/aggregateUsage tokens+cost, git branch), messages_path. This is the best-structured of the lot: id, cwd, model, first prompt, tokens and cost are all in one small JSON.
- Messages file is one JSON document rewritten wholesale (not JSONL) at each iteration_end, so reading the last assistant message means parsing the whole file; may be torn mid-write (non-atomic writeFileSync).
- id format `<epoch-ms>_<5 random>`, e.g. `1779126186341_2vo9c`. No title field seen; use `prompt`.
- Resume: `cline --id <session-id> [prompt]`; interactive `/history`; `cline history` (alias `h`). Bug: `--id` + `--json` rejected (https://github.com/cline/cline/issues/10856 , https://github.com/cline/cline/issues/13239); interactive resume works. No `--continue`-style flag documented. `-c/--cwd <path>` sets directory.
- Auto-approve: `--auto-approve <true|false>`; default is true outside ACP mode, so it is already "yolo" by default (explicit `--auto-approve false` is the safe one).
- Local; global store; cwd in manifest.
- Rating: Medium. Path/format documented only by third parties and changed between 2.x and 3.x (docs even disagree: one page says the sessions dir is SQLite). Directory-scan of manifests is cheap though.

## Amp CLI (Sourcegraph)

- Threads are the unit; CLI: `amp threads list`, `amp threads continue <threadId>` (and `/continue` in the TUI), `amp threads export`, `amp threads list --include-archived --json` (https://openusage.sh/docs/providers/amp/ , search results citing https://ampcode.com/docs/cli ; official page was not fetchable here, so flags are UNVERIFIED against ampcode.com).
- Location: `$XDG_DATA_HOME/amp/threads/<thread_id>.json`, default `~/.local/share/amp/threads/` (https://github.com/block/thread-manager-for-amp , https://openusage.sh/docs/providers/amp/). Billing ledger `~/.local/share/amp/ledger.jsonl` (one line per billed response, key `toMessageId`, `credits`/`cost`, optional tokens/model).
- Thread JSON fields: `id`, `title`, `created`, `messages[]` (role; assistant messages carry `usage` with input/output/cache_read/cache_write in snake or camel case, timestamps), `env`/cwd/trees context (exact key for cwd needs checking against a real file; thread-manager-for-amp says env/cwd/trees). Model: per-message, not confirmed.
- Cloud: threads are server-side objects (shareable, "save and share your interactions"; thread-manager notes mention cloud-based storage), with a local JSON cache. A thread created on another machine may be absent locally; `amp threads continue` presumably fetches it. Local files therefore cover only threads touched on this machine.
- Resume: `amp threads continue <T-id>` (id looks like `T-<uuid>`: UNVERIFIED). Headless: `amp -x --stream-json` (also `threads continue <id> -x --stream-json`).
- Auto-approve: older `--dangerously-allow-all`; per the Amp news (https://ampcode.com/news/neo , via search summary only) tools now run without approval by default unless `amp.permissions` configured, so no flag needed. UNVERIFIED.
- Rating: Medium. Local JSON is easy to read; risk is that the cloud is the source of truth, no documented stable local schema, and CLI is being "rebuilt" (Neo). Needs a real install to pin the schema and cwd key.

## Aider

- Persistence: no session registry. Aider writes plain logs in the project: `.aider.chat.history.md` (`--chat-history-file`, env `AIDER_CHAT_HISTORY_FILE`), `.aider.input.history` (prompt_toolkit FileHistory), optional `.aider.llm.history` (`--llm-history-file`, default off). Defaults land in cwd (git root in practice), see https://aider.chat/docs/config/options.html and https://raw.githubusercontent.com/Aider-AI/aider/main/aider/io.py .
- Format: markdown. File starts a block per run with `# aider chat started at YYYY-MM-DD HH:MM:SS`; user lines are `#### text`; assistant text is raw markdown; tool/system output is `> ` blockquotes (io.py).
- No id, title, model, cost or token record in that file (cost/tokens print as `> Tokens: ... Cost: ...` blockquote lines, parseable by regex but fragile). Model/cwd are not recorded, only implied by file location.
- Resume: `aider --restore-chat-history` (default off) reloads old messages from the history file; `aider --message ...` is one-shot. There is no `--resume <id>`; "sessions" = runs separated by the `# aider chat started at` header, and you cannot resume a specific one.
- Auto-approve: `--yes-always` (env `AIDER_YES_ALWAYS`).
- Discovery problem: no global index. We would need to scan the filesystem for `.aider.chat.history.md` (e.g. `find ~ -name .aider.chat.history.md`, or use the other aider artefact `~/.aider/analytics.json`, which holds no paths) and treat each file as one "session" per project.
- Rating: Hard. Resume is "cd to project, run `aider --restore-chat-history`", which is easy, but discovery and metadata are heuristic. Could ship as a degraded "projects with aider history" row set; do not promise parity.

## Closely related CLIs found (brief; mostly from memory/search, not fetched in detail)

- OpenCode (sst/opencode): `~/.local/share/opencode/opencode.db` (SQLite: `session`, `message`, `part`) since v1.14+/1.17; legacy JSON at `~/.local/share/opencode/storage/session/**`. Resume `opencode --continue` / `--session <id>`. Same adapter as Kilo. Easy-Medium. Sources: https://vibe-replay.com/blog/opencode-local-storage/ , https://github.com/ccusage/ccusage/issues/966 .
- OpenAI Codex CLI: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (Kilo's docs confirm the `~/.codex/sessions/` path); `codex resume [id]`, `codex resume --last`; yolo `--dangerously-bypass-approvals-and-sandbox`/`--yolo`. JSONL with a session_meta first line (id, cwd, timestamp). Likely Easy and the best next target after Claude; details UNVERIFIED this session (from memory), fetch https://github.com/openai/codex before coding.
- Gemini CLI: `~/.gemini/tmp/<project-hash>/chats/*.json` (hash of cwd, so cwd is not directly recoverable without hashing candidates); `--resume`; `--yolo`. UNVERIFIED; Medium.
- Others not researched: Factory Droid, Qwen Code (Gemini CLI fork), Crush (charmbracelet; SQLite in project `.crush/`), Cursor CLI, Kiro CLI.

## Cross-cutting recommendations

1. Add a thin adapter interface with fields: id, title, cwd, last_active, model, first/last prompt, last assistant msg, cost, tokens, size, `resume_argv(id, yolo)`. Order of effort: Codex, Copilot, Goose, OpenCode/Kilo, Cline, Amp, Aider.
2. Open SQLite stores read-only and tolerate missing columns; Goose, Kilo/OpenCode and Cline all changed storage format within the last year.
3. Resume commands to hand to exec:
   - Copilot: `copilot --resume=<id>` (verify) ; yolo `--yolo`
   - Goose: `goose session --resume --session-id <id>` ; yolo `GOOSE_MODE=auto` env
   - Kilo: `kilo --session <id>` (verify) or `kilo -c` ; yolo: toggle only
   - OpenCode: `opencode --session <id>`
   - Cline: `cline --id <id>` ; yolo is default (`--auto-approve true`)
   - Amp: `amp threads continue <id>` ; yolo is default / `--dangerously-allow-all`
   - Aider: `aider --restore-chat-history` in project ; yolo `--yes-always`
4. Items to verify on a real install before coding: Copilot `--resume=<id>`, Kilo `--session`, Amp thread cwd key and `T-` id, Cline 3.x manifest paths.

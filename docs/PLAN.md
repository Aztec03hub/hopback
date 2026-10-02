# Plan: multi-harness, multi-host sessions

Status (2026-10-01): phases 1 to 5 are done; the name is **hopback** (`NAME` below). Next is phase 6.

| Phase | State | Notes |
|---|---|---|
| 1. Refactor into adapters | done | `hopback/adapters/`, `Source` in `sources.py` |
| 2. Windows Claude | done | Found via `/mnt/c/Users/<you>`; resumed through PowerShell. Verified end to end against a real Windows store; the real Windows sessions' agents are not installed on Windows, so the wrapper's "not installed" path was exercised, plus a positive control with a Windows program |
| 3. Codex (WSL + Windows) | done | Handles both rollout event formats (`user_message` and `item_completed`) and the `\\?\` path prefix |
| 4. Hermes | done | Every profile; `-p` always passed; cron has its own toggle |
| 5. UI | done, minus group-by-directory | Tabs, host filter, two toggles, uniform preview, ranked search. Group-by-directory (Option D) deferred |
| 6. Tier-2 adapters | next | Start with Qwen Code (closest format) and Copilot CLI |

Decisions taken: default shows every host, clearly labelled (SOURCE column, host counts, CTRL-O filter); Hermes cron runs get their own CTRL-R toggle; repo renamed after phase 1.
Research sources: `docs/research/*.md` (four reports, 2026-10-01).

## 1. Name

| Candidate | Finding | Verdict |
|---|---|---|
| `recall` | PyPI `recall` is taken (an RPC framework). The bigger problem is **Windows Recall**, Microsoft's flagship Windows 11 feature: the top GitHub hits for "recall" are about removing it, and OpenRecall (2.9k stars) is an open-source clone of it. Our users are on Windows and WSL, so the name collides exactly where we live. | Taken, avoid |
| `ai-sessions` | PyPI `ai-sessions` 3.7.1 is "A terminal browser and cross-harness bridge for Codex, Claude Code, and OpenCode", a **direct competitor doing the same job**. | Taken, avoid |
| `hopback` | Not on PyPI. The top GitHub match is a 1-star unrelated repo. | Free |
| `threadpick` | Not on PyPI. No GitHub repos. | Free |
| `lastword` | Not on PyPI. The top GitHub match has 4 stars. | Free |
| `agentdeck` | Not on PyPI, but a 254-star GitHub "AgentDeck" exists. | Avoid |

Recommendation: **`hopback`**. "Hop back into any agent session, on any OS" is short and typeable. Keep `claude-sessions` as a compatibility alias.

## 2. Harness support list (ranked)

Ratings come from the research reports. "Local" means the session is installed on this laptop; every other entry is checked from docs only and must be verified against a real install before an adapter is written.

| Tier | Harness | Store | Resume | Notes |
|---|---|---|---|---|
| **1: have it, Easy** | Claude Code | `~/.claude/projects/*/*.jsonl` | `claude --resume ID` | Already supported. Both WSL and Windows stores exist. |
| | OpenAI Codex CLI | `~/.codex/state_5.sqlite` (`threads`) + rollout JSONL | `codex resume ID` | Local: 104 sessions in WSL, and a Windows store exists too. 84 of them are subagents (hide by default). Records tokens, not cost. |
| | Hermes Agent | `~/.hermes/state.db` + `profiles/*/state.db` | `hermes --resume ID` | Local: 290 + 277. Hide the cron, subagent and kanban sources by default. Restores cwd on its own. |
| **2: Easy, popular** | Qwen Code | `~/.qwen/projects/<cwd>/chats/*.jsonl` | `qwen --resume ID` | Close to Claude's format |
| | GitHub Copilot CLI | `~/.copilot/session-state/<uuid>/` | `copilot --resume=ID` | |
| | Goose | `~/.local/share/goose/sessions/sessions.db` | `goose session -r --session-id ID` | Records real cost |
| | Mistral Vibe, Grok Build, Pi, Continue `cn`, Kiro CLI | JSON/JSONL per session | `--resume ID` style | Pi's format is the closest to Claude Code's |
| **3: Medium** | Gemini CLI | `~/.gemini/tmp/<slug>/chats/*.jsonl` | `gemini --resume ID` | Two directory-naming schemes; 30-day retention |
| | OpenCode, Kilo CLI | SQLite (same schema) | `opencode -s ID` / `kilo` | Records exact cost and lines changed |
| | Crush | one SQLite DB per project + a registry | `crush -s ID` | |
| | Cline CLI, Amp, Cursor CLI, Factory Droid, OpenHands, Kimi Code, Rovo Dev | various | various | Layouts unstable or cloud-backed |
| **Skip for now** | Aider (no session ids), Plandex (no resume), Letta (server-side), Auggie/Warp/Junie/Devin (undocumented), Antigravity (binary protobuf), `llm`/aichat/mods (chat tools with no project folder) | | | Revisit on request |

## 3. Architecture

One small idea carries everything: a **source** is a *(host, harness, store root)* triple.

```
Source(host="WSL", harness="claude", root="/home/me/.claude")
Source(host="Windows", harness="claude", root="/mnt/c/Users/me/.claude")
Source(host="WSL", harness="codex", root="/home/me/.codex")
Source(host="Windows", harness="codex", root="/mnt/c/Users/me/.codex")
Source(host="WSL", harness="hermes", root="/home/me/.hermes")
```

- **One adapter per harness.** Each is a module that answers three questions: `list(root) -> rows`, `preview(row) -> fields`, and `resume_cmd(row, yolo) -> argv`. Every row uses the same keys we already have: id, name, cwd, mtime, size, role, cost, lines changed, model, branch, plus `host` and `harness`.
- **Hosts are discovered, not configured.**
  - When running in WSL, also check `/mnt/c/Users/<user>/` for each harness's Windows store. The Windows username comes from `cmd.exe /c echo %USERNAME%`.
  - When running on Windows, also check `\\wsl.localhost\<distro>\home\<user>\` for each distro (this is the later Windows port; see §6).
  - A config file (`~/.config/NAME/sources.toml`) adds or disables sources by hand, for example a second WSL distro or a mounted remote home.
- **Cross-OS resume.** Resuming a Windows session from WSL converts its cwd with `wslpath -w` and runs `pwsh.exe -NoExit -Command "Set-Location '<win cwd>'; claude --resume ID"` in the current terminal. A Windows path has no meaning inside WSL, so it must not be passed to a WSL `chdir`. The `--doctor` flag reports which hosts and harnesses were found and how each would be resumed.
- **Reading safely.** SQLite stores are opened read-only (`mode=ro`), because they are live WAL databases and other programs are writing to them. Malformed JSONL lines are skipped. Each adapter is wrapped so that one broken harness shows a warning in the header instead of crashing the picker.
- **Cost.** Use an exact recorded cost when a harness writes one (Claude when the session exited, Goose, OpenCode, Crush). Otherwise apply the measured-rate estimate already shipped for Claude, generalised to any harness that records tokens per reply (Codex, Qwen, Gemini). Show `≈` whenever the cost is estimated.

## 4. UI/UX revamp: options

Two entry modes are required, as requested:

```
NAME                 everything, all hosts, all harnesses
NAME claude          one harness (also: NAME codex, NAME hermes ...)
NAME --host windows  one host; combine: NAME codex --host wsl
```

`claude-sessions` keeps working as an alias for `NAME claude --host <current>`, so the current behaviour is unchanged.

How to show *where* each row comes from:

**Option A: SOURCE column with short, colour-coded tags** (recommended)

```
 LAST ACTIVE        SOURCE       NAME                          DIRECTORY             SIZE
 4m ago             claude·wsl   Rate limiter for public API   ~/projects/acme-api   909 KB
 1h ago, 3:10 PM    codex·win    Fix installer paths           C:\src\setup          2.1 MB
 3h ago, 1:02 PM    hermes·wsl   Morning digest                ~                     44 KB
```

Each harness gets one colour (Claude orange, Codex green, Hermes violet...) and each host gets a dim suffix. It adds one 11-character column and keeps rows scannable. It works in both modes; in single-harness mode the column shrinks to the host alone.

**Option B: sidebar of sources, with checkboxes and counts**

```
┌ Sources ────────┐┌ 412 sessions ─────────────────────────────────────┐
│ ▣ All      412  ││ 4m ago    ● Rate limiter for public API   ~/acme  │
│ ▣ Claude   231  ││ 1h ago    ● Fix installer paths   C:\src\setup    │
│   ▣ WSL    204  ││ ...                                               │
│   ▣ Win     27  ││                                                   │
│ ▣ Codex    104  ││                                                   │
│ ▣ Hermes    77  ││                                                   │
└─────────────────┘└───────────────────────────────────────────────────┘
```

Clicking a row in the sidebar, or pressing a number key, toggles that source. This scales best once there are 6 or more harnesses and is very discoverable, but it takes about 18 columns of width. The row marker (`●`) uses the harness colour.

**Option C: tabs across the top**

```
 All 412 │ Claude 231 │ Codex 104 │ Hermes 77 │      host: [WSL+Win ▾]
```

`TAB` cycles through the tabs. This maps directly onto the two modes: the All tab is mode 1 and the other tabs are mode 2. It's clean, but it hides the host breakdown behind a dropdown.

**Option D: group by project directory, sources nested**

```
▾ ~/projects/acme-api        (5 sessions, 3 harnesses)
    4m ago    claude·wsl   Rate limiter for public API
    2d ago    codex·win    Rate limiter spike
```

This answers "what did *any* agent do in this repo?", which none of the other options do. It works best as a toggle view (`CTRL-G`), not as the default.

**Recommendation:** use Option A as the default. Add Option C's tab bar as the way to filter (it also shows the counts), and make Option D a toggle. Skip B unless the number of harnesses grows past about 8.

Changes shared by all the options:
- The search also matches the source (`codex win acme`). This needs the fzf-style search to treat a space as an AND, which fixes the current space limitation at the same time.
- The preview gains `harness` and `host` rows, and shows the exact resume command that will run.
- The toggles for hidden agent, subagent and cron sessions become one menu, `CTRL-T`, applied per harness.
- When the same working directory appears as both a Windows path and a WSL path, it is shown in the current OS's form (`C:\src` ↔ `/mnt/c/src`) and the original form appears in the preview.

## 5. Phases

1. **Refactor, with no behaviour change.**
   - Split the current code into `core` (picker and formatting) and `adapters/claude.py`.
   - Add the `Source` triple, host discovery and the `SOURCE` column.
   - Existing screenshots must look the same, apart from the new column.
2. **Windows Claude.** Discover the Windows store and add cross-OS resume through `pwsh.exe`. Verify by resuming one real Windows session.
3. **Codex adapter (WSL + Windows).**
   - Read the `threads` table, then the rollout JSONL for messages.
   - Use the subagent flag for `role`.
   - Add a token-based cost estimate.
4. **Hermes adapter.**
   - Read `state.db` and the profile databases.
   - Use the source field for role (cron, subagent).
   - Resume with `-p <profile>` where it applies.
5. **UI:** tabs, the source filter, single-harness modes and the group-by-directory view. Regenerate the screenshots using fake stores for each harness.
6. **Tier-2 adapters**, one per PR, each starting with "inspect one real session file": Qwen, Copilot, Goose, Pi, Vibe, Grok, Continue and Kiro.
7. **Tier-3 adapters** as there is demand.
8. **Native Windows support:** the reverse direction, reading WSL stores from Windows. Optional; the README currently states that Windows is not supported.

Every phase needs a runnable check: a per-adapter fixture (a tiny fake store) and an assert-based test that `list`, `preview` and `resume_cmd` return the expected rows and command lines. These fixtures are also used to build the screenshots.

## 6. Open questions (resolved)

1. The name: **hopback**.
2. Default: **every host**, well organized and labelled.
3. Hermes cron runs: **their own toggle** (CTRL-R).
4. Repo renamed **after phase 1** landed.

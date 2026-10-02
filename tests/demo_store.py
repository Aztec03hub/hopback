"""A FAKE multi-harness, multi-host session store, for tests and screenshots.

Never point hopback's screenshots at a real home: they are public. This builds
invented sessions for Claude Code, Codex and Hermes in a throwaway home, plus a
second throwaway directory standing in for the Windows user profile, in the
same on-disk formats the real tools write (see docs/research/).

    build(home, win_home)   writes everything; returns a dict of notable ids
"""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

NOW = time.time()
H = 3600
D = 24 * H


def _jsonl(path, recs, age):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Compact separators, as Claude Code writes: the adapter's cost lookup
    # matches '"type":"cost-state"' byte-for-byte.
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in recs) + "\n")
    os.utime(path, (NOW - age, NOW - age))


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

# (age, cwd under ~, title, renamed, first prompt, last prompt, reply, kind, branch, cost, pad KB)
CLAUDE = [
    (4 * 60, "projects/acme-api", "Rate limiter for public API", True,
     "Add per-key rate limiting to the public API", "Now add a Redis backend for the limiter",
     "Done. The limiter now uses Redis with a sliding window; 14 tests pass.", "live",
     "feat/rate-limit", 3.42, 900),
    (50 * 60, "projects/acme-web", "Dark mode toggle", False,
     "Add a dark mode toggle to the settings page", "Make it follow the system theme by default",
     "It now reads prefers-color-scheme on first load and remembers overrides.", "cli", "main",
     1.18, 300),
    (3 * H, "projects/acme-api", "Migrate auth to OAuth 2.1", False,
     "Plan the OAuth 2.1 migration", "Write the migration guide for clients",
     "Guide written to docs/oauth-migration.md.", "lead", "oauth21", 8.90, 6200),
    (3 * H + 200, "projects/acme-api", "", False, "Review token refresh", "",
     "Refresh path verified.", "team", "", 0, 120),
    (3 * H + 400, "projects/acme-api", "", False, "Audit scopes", "",
     "Scopes audited.", "team", "", 0, 140),
    (5 * H, "dotfiles", "Tidy zsh config", True,
     "Clean up my zshrc", "Split aliases into their own file",
     "Aliases moved to ~/.zsh/aliases.zsh.", "cli", "master", 0.37, 60),
    (27 * H, "projects/ml-notebook", "Nightly eval run", False,
     "Run the eval suite", "", "Eval complete.", "sdk", "", 0, 200),
    (3 * D, "projects/acme-web", "Fix flaky checkout test", False,
     "The checkout e2e test fails one run in five", "Is it the network mock?",
     "Yes: the mock resolved before the route registered. Fixed with an await.", "cli",
     "fix/flaky", 0.94, 500),
    (9 * D, "projects/blog", "Blog post: profiling Python", False,
     "Help me outline a post on profiling", "Tighten the intro",
     "Intro cut to three sentences.", "cli", "drafts", 0.52, 180),
    (20 * D, "projects/infra", "Kubernetes upgrade notes", False,
     "What changes in the 1.31 upgrade?", "Summarize the breaking ones",
     "Two breaking changes affect us: ...", "cli", "", 0.88, 400),
]


def _claude_session(projects, cwd, encoded, age, title, renamed, first, last, reply, kind,
                    branch, cost, pad, model="claude-opus-5-5"):
    sid = str(uuid.uuid4())
    ep = "sdk-py" if kind == "sdk" else "cli"
    recs = [{"type": "user", "entrypoint": ep, "cwd": cwd, "gitBranch": branch,
             "message": {"role": "user", "content": first}}]
    if kind == "team":
        recs.append({"type": "user", "teamName": "oauth-migration",
                     "agentName": "reviewer" if pad == 120 else "auditor", "cwd": cwd})
    recs.append({"type": "assistant", "cwd": cwd,
                 "message": {"role": "assistant", "model": model,
                             "content": [{"type": "text", "text": "Working on it."}]}})
    recs += [{"type": "progress", "data": "x" * 1000}] * pad
    if last:
        recs.append({"type": "user", "cwd": cwd, "message": {"role": "user", "content": last}})
        recs.append({"type": "last-prompt", "lastPrompt": last})
    recs.append({"type": "assistant", "cwd": cwd,
                 "message": {"role": "assistant", "model": model,
                             "content": [{"type": "text", "text": reply}]}})
    if kind == "live":
        # Still running: no cost record yet, only per-reply usage, so the
        # picker shows an estimate priced from the other sessions' records.
        recs.append({"type": "assistant", "cwd": cwd, "timestamp": "2026-01-01T00:00:00Z",
                     "message": {"id": "msg_live", "role": "assistant", "model": model,
                                 "usage": {"input_tokens": 9000, "output_tokens": 61000,
                                           "cache_read_input_tokens": 2400000,
                                           "cache_creation_input_tokens": 180000},
                                 "content": [{"type": "text", "text": reply}]}})
    elif cost:
        recs.append({"type": "cost-state", "totalCostUSD": cost,
                     "totalLinesAdded": int(cost * 210), "totalLinesRemoved": int(cost * 60),
                     "modelUsage": {model: {
                         "inputTokens": 1000, "outputTokens": int(cost * 8000),
                         "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                         "costUSD": cost}}})
    if title:
        recs.append({"type": "ai-title", "aiTitle": title})
        if renamed:
            recs.append({"type": "custom-title", "customTitle": title})
    _jsonl(projects / encoded / f"{sid}.jsonl", recs, age)
    return sid


def build_claude(home, ids):
    projects = home / ".claude" / "projects"
    for age, sub, *rest in CLAUDE:
        cwd = f"{home}/{sub}"
        Path(cwd).mkdir(parents=True, exist_ok=True)
        sid = _claude_session(projects, cwd, cwd.replace("/", "-"), age, *rest)
        kind = rest[5]
        ids.setdefault(f"claude-{kind}", sid)
        if kind == "lead":
            t = home / ".claude" / "teams" / "oauth-migration"
            t.mkdir(parents=True, exist_ok=True)
            (t / "config.json").write_text(json.dumps(
                {"name": "oauth-migration", "leadSessionId": sid, "members": [1, 2, 3]}))
    # No cwd on any record: decoded from the folder name, marked "?".
    sid = str(uuid.uuid4())
    _jsonl(projects / f"{home}/projects/legacy".replace("/", "-") / f"{sid}.jsonl", [
        {"type": "user", "entrypoint": "cli",
         "message": {"role": "user", "content": "Port the old cron jobs"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Ported 6 jobs to systemd timers."}]}},
        {"type": "ai-title", "aiTitle": "Port legacy cron jobs"}], 12 * D)
    ids["claude-guessed"] = sid


def build_claude_windows(win_home, ids):
    projects = win_home / ".claude" / "projects"
    ids["claude-win"] = _claude_session(
        projects, "C:\\Users\\dev\\src\\installer", "C--Users-dev-src-installer", 2 * H,
        "Fix MSI installer paths", True, "The MSI installs to the wrong Program Files",
        "Add a test for the 32-bit case", "Added; both architectures now pass.", "cli", "main",
        0.81, 150)


# --------------------------------------------------------------------------
# OpenAI Codex
# --------------------------------------------------------------------------

CODEX_SCHEMA = """
CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, source TEXT NOT NULL,
  model_provider TEXT NOT NULL, cwd TEXT NOT NULL, title TEXT NOT NULL,
  sandbox_policy TEXT NOT NULL, approval_mode TEXT NOT NULL,
  tokens_used INTEGER NOT NULL DEFAULT 0, has_user_event INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0, archived_at INTEGER, git_sha TEXT, git_branch TEXT,
  git_origin_url TEXT, cli_version TEXT NOT NULL DEFAULT '',
  first_user_message TEXT NOT NULL DEFAULT '', agent_nickname TEXT, agent_role TEXT,
  model TEXT, reasoning_effort TEXT, created_at_ms INTEGER, updated_at_ms INTEGER,
  thread_source TEXT, preview TEXT NOT NULL DEFAULT '', name TEXT);
CREATE TABLE thread_spawn_edges (parent_thread_id TEXT NOT NULL,
  child_thread_id TEXT NOT NULL PRIMARY KEY, status TEXT NOT NULL);
"""


def _codex_rollout(path, first, last, reply, new_format, age):
    def user(text):
        if new_format:
            return {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "UserMessage", "content": [{"type": "text", "text": text}]}}}
        return {"type": "event_msg", "payload": {"type": "user_message", "message": text}}

    def agent(text):
        if new_format:
            return {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "AgentMessage", "content": [{"type": "Text", "text": text}]}}}
        return {"type": "event_msg", "payload": {"type": "agent_message", "message": text}}

    recs = [{"type": "session_meta", "payload": {}},
            # Injected context, which must NOT be shown as the user's prompt.
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text",
                                                               "text": "<environment_context>"}]}},
            user(first), agent("On it."), user(last), agent(reply),
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 1_840_000, "cached_input_tokens": 1_610_000,
                                      "output_tokens": 42_000}}}}]
    _jsonl(path, recs, age)


def build_codex(home, ids, windows=False):
    root = home / ".codex"
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / "state_5.sqlite")
    con.executescript(CODEX_SCHEMA)
    if windows:
        threads = [("Package the Windows build", "user", "cli", "C:\\Users\\dev\\src\\desktop",
                    "Make a signed Windows build", "Now bump the version to 2.4",
                    "Bumped to 2.4.0 and rebuilt; the installer is in dist/.", 30 * 60, None, True)]
    else:
        threads = [
            ("Add pagination to the users API", "user", "cli", f"{home}/projects/acme-api",
             "Add cursor pagination to GET /users", "Also cover the empty-page case",
             "Covered: an empty page returns an empty list and no cursor.", 20 * 60,
             "Users API pagination", False),
            ("Profile the search endpoint", "user", "cli", f"{home}/projects/acme-web",
             "Search is slow, find out why", "Try the trigram index",
             "The trigram index cut p95 from 840 ms to 95 ms.", 26 * H, None, True),
            ("", "subagent", "cli", f"{home}/projects/acme-web",
             "Benchmark the three index options", "", "Trigram wins on every query shape.",
             26 * H + 300, None, True),
            ("Nightly dependency audit", "user", "exec", f"{home}/projects/acme-api",
             "Audit dependencies for advisories", "", "No new advisories.", 2 * D, None, False),
        ]
    parent = None
    for title, tsource, source, cwd, first, last, reply, age, name, newfmt in threads:
        tid = str(uuid.uuid4())
        rollout = root / "sessions" / f"rollout-{tid}.jsonl"
        _codex_rollout(rollout, first, last or first, reply, newfmt, age)
        ts = NOW - age
        # A real Windows store records C:\ paths; the demo records the fake
        # location so the preview can read it on any machine.
        con.execute(
            "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, model_provider,"
            " cwd, title, sandbox_policy, approval_mode, tokens_used, first_user_message,"
            " agent_nickname, model, reasoning_effort, updated_at_ms, thread_source, preview,"
            " name, git_branch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, str(rollout), int(ts - 3600), int(ts),
             '{"subagent":{}}' if tsource == "subagent" else source, "openai", cwd,
             title or first, "{}", "never", 1_882_000, first,
             "Hume" if tsource == "subagent" else None, "gpt-5.6-terra", "medium",
             int(ts * 1000), tsource, first[:60], name, "main"))
        if tsource == "subagent" and parent:
            con.execute("INSERT INTO thread_spawn_edges VALUES (?,?,?)", (parent, tid, "done"))
        if tsource == "user" and source == "cli":
            parent = parent or tid
        ids.setdefault(f"codex-{'win' if windows else source + '-' + tsource}", tid)
    con.commit()
    con.close()


# --------------------------------------------------------------------------
# Hermes Agent
# --------------------------------------------------------------------------

HERMES_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, display_name TEXT,
  model TEXT, parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL,
  message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0, cwd TEXT, git_branch TEXT,
  estimated_cost_usd REAL, actual_cost_usd REAL, title TEXT, title_source TEXT,
  last_activity_at REAL, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL);
"""


def build_hermes(root, sessions, ids, prefix):
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / "state.db")
    con.executescript(HERMES_SCHEMA)
    parent = None
    for n, (source, title, cwd, age, first, last, reply, cost) in enumerate(sessions):
        sid = time.strftime("%Y%m%d_%H%M%S", time.localtime(NOW - age)) + f"_{prefix}{n:02d}"
        msgs = [("user", first), ("assistant", "Looking into it."), ("user", last or first),
                ("assistant", reply)]
        con.execute(
            "INSERT INTO sessions (id, source, model, parent_session_id, started_at,"
            " last_activity_at, message_count, tool_call_count, input_tokens, output_tokens,"
            " cache_read_tokens, cwd, estimated_cost_usd, title, title_source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, source, "deepseek-v4-flash", parent if source == "subagent" else None,
             NOW - age - 900, NOW - age, len(msgs), 12, 412_000, 18_300, 3_900_000, cwd, cost,
             title, "user" if source == "cron" else "llm"))
        con.executemany("INSERT INTO messages (session_id, role, content, timestamp)"
                        " VALUES (?,?,?,?)", [(sid, r, c, NOW - age) for r, c in msgs])
        if source == "cli":
            parent = parent or sid
        ids.setdefault(f"hermes-{prefix}-{source}", sid)
    con.commit()
    con.close()


def build(home, win_home):
    ids = {}
    build_claude(home, ids)
    build_claude_windows(win_home, ids)
    build_codex(home, ids)
    build_codex(win_home, ids, windows=True)
    proj = f"{home}/projects"
    build_hermes(home / ".hermes", [
        ("cli", "Draft the Q3 roadmap", f"{proj}/acme-api", 40 * 60,
         "Turn these notes into a Q3 roadmap", "Add a risks section",
         "Added a risks section with three items and owners.", 0.12),
        ("subagent", "Summarize customer interviews", f"{proj}/acme-api", 45 * 60,
         "Summarize the 12 interview transcripts", "", "Top theme: onboarding friction.", 0),
        ("cron", "Morning digest", None, 7 * H, "Send the morning digest", "",
         "Digest sent: 4 PRs, 2 incidents, 1 release.", 0),
        ("cron", "Morning digest", None, 31 * H, "Send the morning digest", "",
         "Digest sent: 2 PRs, 0 incidents.", 0),
        ("telegram", "Weekend reading list", None, 4 * D, "Recommend three books on systems",
         "", "Designing Data-Intensive Applications, ...", 0),
    ], ids, "d")
    build_hermes(home / ".hermes" / "profiles" / "writer", [
        ("cli", "Short story: the lighthouse keeper", f"{home}/writing", 6 * D,
         "Write a 500-word story about a lighthouse keeper", "Make the ending ambiguous",
         "Rewrote the last paragraph so the light's return is never explained.", 0),
    ], ids, "w")
    (home / "writing").mkdir(parents=True, exist_ok=True)
    return ids

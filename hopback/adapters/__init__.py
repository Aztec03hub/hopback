"""One module per harness. Each module provides:

    NAME, LABEL, ASSISTANT, EXE   short id, display name, who replies, its command
    roots(home) -> [Path]         store roots under a user home, if any exist
    collect(root, limit, here_only, deep, include_teams=..., include_scratch=...,
            include_empty=..., limit_counts_visible=..., include_archived=..., only=...)
                                  -> (rows, total sessions in the store), or with a
                                  third item, [problem, ...]: what went wrong without
                                  costing the list (shown above it). `only`, a set of
                                  ids, limits the rows to those.
    details(root, id) -> dict     title, fields [(label, value)], prompt, reply,
                                  opening; None if the id is not in this store
    resume_cmd(root, row, yolo)   argv that resumes the session
    ids(root), count(root)

A row is a dict with: id, name, cwd, mtime, bytes (or size_text), role, team,
agent, named, guessed. `role` is "" for a session a person started, or one of
the badges in ROLES. The picker adds `source` itself.
"""
from . import claude, codex, hermes

ADAPTERS = [claude, codex, hermes]
BY_NAME = {a.NAME: a for a in ADAPTERS}

# Badge -> legend text. AGENT roles hide behind CTRL-T, SCHEDULED behind CTRL-R,
# LEFTOVER behind CTRL-B.
ROLES = {
    "lead": "led an agent team",
    "team": "was a teammate",
    "sub": "subagent",
    "sdk": "Agent SDK run",
    "exec": "codex exec run",
    "task": "kanban task",
    "cron": "scheduled run",
    "chat": "chat gateway",
    "ide": "started in an editor",
    "spawn": "started by another session's Claude",
    "copy": "older copy of a session that moved",
    "job": "background-job scratch run",
}
AGENT = {"team", "sub", "sdk", "exec", "task", "spawn"}
SCHEDULED = {"cron"}
LEFTOVER = {"copy", "job"}

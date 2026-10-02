"""Hermes Agent adapter.

Hermes keeps sessions and their messages in SQLite: ~/.hermes/state.db for the
default profile and ~/.hermes/profiles/<name>/state.db for each named profile.
Every database is its own store root, so each profile is listed separately and
resumed with `hermes -p <profile> --resume <id>`. The profile flag is always
passed, even for "default": otherwise a sticky `hermes profile use` choice
would resume against a different profile's database and not find the id.

Field notes, measured on real stores (see docs/research/local-codex-hermes.md):
- `source` says how a session started: cli, subagent, cron (scheduled jobs),
  kanban (board tasks), or a chat gateway such as telegram.
- Compression forks a session into a child and can leave the parent with no
  messages; those are hidden as empty unless -e is given.
- Cost columns exist but are only filled for providers Hermes can price.
"""
import time
from contextlib import closing
from pathlib import Path

from ..readers import open_ro

NAME = "hermes"
LABEL = "Hermes Agent"
ASSISTANT = "Hermes"
EXE = "hermes"

ROLE = {"cli": "", "subagent": "sub", "cron": "cron", "kanban": "task"}


def roots(home):
    base = home / ".hermes"
    found = [base] if (base / "state.db").is_file() else []
    found += sorted(p.parent for p in (base / "profiles").glob("*/state.db"))
    return found


def profile(root):
    return root.name if root.parent.name == "profiles" else "default"


def _db(root):
    return open_ro(root / "state.db")


def _role(source):
    # Chat gateways (telegram, slack, ...) are conversations a person started,
    # so they stay visible, tagged so they are not mistaken for terminal work.
    return ROLE.get(source, "chat")


def _when(r):
    return r["last_activity_at"] or r["ended_at"] or r["started_at"]


def collect(root, limit, here_only, deep, include_teams=True, include_scratch=False,
            include_empty=False, limit_counts_visible=False, include_archived=False, **_):
    with closing(_db(root)) as con:
        rows = con.execute("SELECT id, source, title, title_source, display_name, cwd, "
                           "started_at, ended_at, last_activity_at, message_count, archived "
                           "FROM sessions").fetchall()
    total = len(rows)
    here = str(Path.cwd().resolve())
    out, counted = [], 0
    for r in sorted(rows, key=_when, reverse=True):
        if limit and counted >= limit:
            break
        role = _role(r["source"])
        if r["archived"] and not include_archived:
            continue
        if not include_empty and not r["message_count"]:
            continue
        if not include_teams and role in ("sub", "task"):
            continue
        cwd = r["cwd"] or ""
        if here_only and cwd != here:
            continue
        if not include_scratch and (cwd == "/tmp" or cwd.startswith("/tmp/")):
            continue
        if not limit_counts_visible or role not in ("sub", "task", "cron"):
            counted += 1
        n = r["message_count"] or 0
        out.append({
            "mtime": _when(r),
            "name": " ".join((r["title"] or r["display_name"] or "-").split()),
            "role": role,
            "team": "",
            "agent": "",
            # No recorded directory (cron and chat sessions): shown as "-", and
            # Hermes resumes wherever it is run.
            "cwd": cwd,
            "id": r["id"],
            "bytes": 0,
            "size_text": f"{n:,} msg" if n < 100_000 else f"{n // 1000}K msg",
            "named": r["title_source"] == "user",
            "guessed": False,
        })
    return out, total


def _text(con, sid, role, newest=True):
    order = "DESC" if newest else "ASC"
    hit = con.execute(f"SELECT content FROM messages WHERE session_id = ? AND role = ? "
                      f"AND content IS NOT NULL AND content != '' ORDER BY id {order} LIMIT 1",
                      (sid, role)).fetchone()
    return hit[0] if hit else None


def _tok(n):
    n = n or 0
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else str(n)


def details(root, sid):
    with closing(_db(root)) as con:
        r = con.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if r is None:
            return None
        prompt = _text(con, sid, "user")
        reply = _text(con, sid, "assistant")
        opening = _text(con, sid, "user", newest=False)
        parent = None
        if r["parent_session_id"]:
            p = con.execute("SELECT title FROM sessions WHERE id = ?",
                            (r["parent_session_id"],)).fetchone()
            parent = f"{p[0]} ({r['parent_session_id']})" if p and p[0] else r["parent_session_id"]
    fields = [("profile", profile(root)),
              ("started by", r["source"]),
              ("directory", r["cwd"] or "(not recorded)")]
    if r["git_branch"]:
        fields.append(("git branch", r["git_branch"]))
    fields.append(("last active", time.strftime("%a %-d %b %Y, %-I:%M %p",
                                                time.localtime(_when(r)))))
    if r["model"]:
        fields.append(("model", r["model"]))
    fields.append(("tokens", f"{_tok(r['input_tokens'])} in "
                             f"({_tok(r['cache_read_tokens'])} cache read) · "
                             f"{_tok(r['output_tokens'])} out"))
    cost = r["actual_cost_usd"] or r["estimated_cost_usd"]
    if cost:
        fields.append(("cost", f"${cost:,.2f}" if r["actual_cost_usd"] else f"≈ ${cost:,.2f}"))
    else:
        fields.append(("cost", "not recorded by Hermes for this provider"))
    fields.append(("size", f"{r['message_count'] or 0:,} messages, "
                           f"{r['tool_call_count'] or 0:,} tool calls"))
    if parent:
        fields.append(("spawned by", parent))
    return {
        "title": " ".join((r["title"] or r["display_name"] or "(untitled)").split()),
        "fields": fields,
        "prompt": prompt,
        "reply": reply,
        "opening": opening,
    }


def resume_cmd(root, row, yolo):
    cmd = ["hermes", "-p", profile(root), "--resume", row["id"]]
    return cmd + ["--yolo"] if yolo else cmd


def ids(root):
    with closing(_db(root)) as con:
        return [r[0] for r in con.execute("SELECT id FROM sessions")]


def count(root):
    with closing(_db(root)) as con:
        return con.execute("SELECT count(*) FROM sessions").fetchone()[0]

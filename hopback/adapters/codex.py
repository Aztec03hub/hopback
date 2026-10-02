"""OpenAI Codex CLI adapter.

Codex keeps an index of every thread in ~/.codex/state_5.sqlite (table
`threads`) and the conversation itself in a "rollout" JSONL file whose path the
index records. The index carries nearly everything the list needs, so listing
is one query; transcripts are read only for the preview, from the end.

Field notes, measured on real stores (see docs/research/local-codex-hermes.md):
- `name` is the user's rename; `title` is the first message, often long.
- `thread_source` is 'user' or 'subagent'; `source` 'exec' marks a
  non-interactive `codex exec` run, the Codex analogue of an SDK run.
- Windows stores record paths as C:\\... and sometimes \\\\?\\C:\\..., which
  paths.to_local() turns into /mnt/c/... when read from WSL.
- Records in a rollout are {"type": ..., "payload": {...}}. The user's own
  words are event_msg/user_message; response_item messages with role user
  are mostly injected context, so they are deliberately not used.
- Codex records tokens, not dollars, so no cost is shown.
"""
import time
from contextlib import closing
from pathlib import Path

from ..fmt import size_str
from ..paths import clean_win, to_local
from ..readers import columns, lines_backwards, loads, open_ro

NAME = "codex"
LABEL = "OpenAI Codex"
ASSISTANT = "Codex"
EXE = "codex"
DANGER_FLAG = "--dangerously-bypass-approvals-and-sandbox"


def roots(home):
    root = home / ".codex"
    return [root] if (root / "state_5.sqlite").is_file() else []


class _Rec(dict):
    """A row whose missing columns read as None: older Codex versions lack
    some of the columns newer ones have, and one absent column must not lose
    the whole store."""

    def __missing__(self, key):
        return None


def _db(root):
    return open_ro(root / "state_5.sqlite")


def _role(r):
    ts = r["thread_source"]
    if ts == "subagent":
        return "sub"
    if r["source"] == "exec":
        return "exec"
    if ts in (None, "", "user"):
        return ""
    # Threads started from an editor or by Codex itself (seen: vscode
    # onboarding_checklist), tagged so they are not taken for terminal work.
    return "ide"


def _name(r):
    name = r["name"] or r["title"] or r["first_user_message"] or r["agent_nickname"] or "-"
    return " ".join(name.split())  # titles are raw first messages, newlines and all


def _when(r):
    ms = r["updated_at_ms"]
    return ms / 1000 if ms else (r["updated_at"] or 0)


def _rollout(r):
    return Path(to_local(r["rollout_path"]))


def collect(root, limit, here_only, deep, include_teams=True, include_scratch=False,
            include_empty=False, limit_counts_visible=False, include_archived=False, **_):
    with closing(_db(root)) as con:
        rows = [_Rec(r) for r in con.execute("SELECT * FROM threads")]
    total = len(rows)
    here = str(Path.cwd().resolve())
    out, counted = [], 0
    for r in sorted(rows, key=_when, reverse=True):
        if limit and counted >= limit:
            break
        role = _role(r)
        if r["archived"] and not include_archived:
            continue
        if not include_teams and role in ("sub", "exec"):
            continue
        cwd = clean_win(r["cwd"]) or ""
        if here_only and to_local(cwd) != here:
            continue
        if not include_scratch and (cwd == "/tmp" or cwd.startswith("/tmp/")):
            continue
        if not include_empty and not (r["title"] or r["first_user_message"] or r["preview"]
                                      or role == "sub"):
            continue
        if not limit_counts_visible or role not in ("sub", "exec"):
            counted += 1
        try:
            size = _rollout(r).stat().st_size
        except OSError:
            size = 0
        out.append({
            "mtime": _when(r),
            "name": _name(r),
            "role": role,
            "team": "",
            "agent": r["agent_nickname"] or "",
            "cwd": cwd,
            "id": r["id"],
            "bytes": size,
            "named": bool(r["name"]),
            "guessed": False,
        })
    return out, total


def _item_text(item):
    parts = item.get("content") or []
    return "\n".join(c.get("text", "") for c in parts if isinstance(c, dict)).strip() or None


def _said(p):
    """("user" | "assistant", text) if this event payload is something a person
    or the agent actually said, else None.

    Two generations of the format coexist: older rollouts write
    user_message / agent_message events, newer ones (0.150+) write
    item_completed events wrapping a UserMessage / AgentMessage item.
    """
    kind = p.get("type")
    if kind == "user_message" and p.get("message"):
        return "user", p["message"]
    if kind == "agent_message" and p.get("message"):
        return "assistant", p["message"]
    if kind == "task_complete" and p.get("last_agent_message"):
        return "assistant", p["last_agent_message"]
    if kind == "item_completed":
        item = p.get("item") or {}
        role = {"UserMessage": "user", "AgentMessage": "assistant"}.get(item.get("type"))
        text = _item_text(item) if role else None
        if text:
            return role, text
    return None


def _messages(path, limit=64 * 1024 * 1024):
    """(last user prompt, last assistant reply, newest token totals) from the
    end of a rollout. Bounded so a corrupt multi-GB file cannot stall the UI."""
    got, tokens = {}, None
    for line in lines_backwards(path, limit=limit):
        if b'"event_msg"' not in line:
            continue
        p = ((loads(line) or {}).get("payload")) or {}
        if p.get("type") == "token_count":
            if tokens is None:
                tokens = (p.get("info") or {}).get("total_token_usage") or None
        else:
            said = _said(p)
            if said and said[0] not in got:
                got[said[0]] = said[1]
        if len(got) == 2 and tokens:
            break
    return got.get("user"), got.get("assistant"), tokens


def _first_prompt(path, max_lines=400):
    try:
        with open(path, "rb") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                if b'"user_message"' in line or b'"UserMessage"' in line:
                    said = _said(((loads(line) or {}).get("payload")) or {})
                    if said and said[0] == "user":
                        return said[1]
    except OSError:
        pass
    return None


def _tok(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else str(n)


def details(root, sid):
    with closing(_db(root)) as con:
        r = con.execute("SELECT * FROM threads WHERE id = ?", (sid,)).fetchone()
        r = _Rec(r) if r is not None else None
        parent = None
        if r is not None and "thread_spawn_edges" in {
                t[0] for t in con.execute("SELECT name FROM sqlite_master")}:
            cols = columns(con, "thread_spawn_edges")
            if {"parent_thread_id", "child_thread_id"} <= cols:
                hit = con.execute("SELECT parent_thread_id FROM thread_spawn_edges "
                                  "WHERE child_thread_id = ?", (sid,)).fetchone()
                parent = hit[0] if hit else None
    if r is None:
        return None
    path = _rollout(r)
    prompt, reply, tokens = _messages(path)
    fields = []
    if r["name"] and r["title"]:
        fields.append(("first message", " ".join(r["title"].split())[:120]))
    fields.append(("directory", clean_win(r["cwd"])))
    if r["git_branch"]:
        fields.append(("git branch", r["git_branch"]))
    fields.append(("last active", time.strftime("%a %-d %b %Y, %-I:%M %p",
                                                time.localtime(_when(r)))))
    model = r["model"] or ""
    if model and r["reasoning_effort"]:
        model += f" ({r['reasoning_effort']} effort)"
    if model:
        fields.append(("model", model))
    if tokens:
        fields.append(("tokens", f"{_tok(tokens.get('input_tokens', 0))} in "
                                 f"({_tok(tokens.get('cached_input_tokens', 0))} cached) · "
                                 f"{_tok(tokens.get('output_tokens', 0))} out"))
    elif r["tokens_used"]:
        fields.append(("tokens", _tok(r["tokens_used"])))
    fields.append(("cost", "not recorded by Codex"))
    try:
        fields.append(("size", size_str(path.stat().st_size)))
    except OSError:
        fields.append(("size", "transcript missing"))
    if r["agent_nickname"]:
        fields.append(("subagent", f"{r['agent_nickname']}"
                                   + (f", spawned by {parent}" if parent else "")))
    if r["source"] == "exec":
        fields.append(("launched", "non-interactively with `codex exec`"))
    return {
        "title": _name(r),
        "fields": fields,
        "prompt": prompt,
        "reply": reply,
        "opening": _first_prompt(path) or r["first_user_message"],
    }


def resume_cmd(root, row, yolo):
    cmd = ["codex", "resume", row["id"]]
    return cmd + [DANGER_FLAG] if yolo else cmd


def ids(root):
    with closing(_db(root)) as con:
        return [r[0] for r in con.execute("SELECT id FROM threads")]


def count(root):
    with closing(_db(root)) as con:
        return con.execute("SELECT count(*) FROM threads").fetchone()[0]

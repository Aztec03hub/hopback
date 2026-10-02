"""Claude Code adapter: reads ~/.claude/projects/<encoded-cwd>/<id>.jsonl.

Every function takes the store ROOT (the .claude directory) rather than reading
a global, so one process can read several Claude stores, e.g. WSL and Windows.
"""
import json
import time
from pathlib import Path

from ..fmt import size_str

NAME = "claude"
DEFAULT_ROOT = Path.home() / ".claude"
# Read at most this much from the end of a file when hunting for fields. Titles
# recur often enough that the tail almost always carries one; --deep covers the
# rare session titled only at the very start. Measured identical on this store.
TAIL_BYTES = 256 * 1024
# Sessions smaller than this are cheap enough to read in full when checking
# whether they contain any conversation at all. Larger ones always do.
EMPTY_PROBE_BYTES = 80 * 1024
DANGER_FLAG = "--dangerously-skip-permissions"


# --------------------------------------------------------------------------
# Session store reading. Unchanged from the fzf implementation.
# --------------------------------------------------------------------------

def tail_text(path, nbytes=TAIL_BYTES):
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > nbytes:
                fh.seek(size - nbytes)
                fh.readline()  # discard the partial line we landed in
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def scan(path, whole_file=False, want_prompt=False):
    """Newest value of each field, found by reading backwards."""
    if whole_file:
        try:
            text = path.read_text("utf-8", "replace")
        except OSError:
            return {}
    else:
        # Preview reads one file on demand and wants fields that sit much
        # further back than the title: this store puts the newest cost-state
        # record ~518 KB from the end of a 24 MB session, outside the list-mode
        # window. List mode stays small because it does this per file.
        text = tail_text(path, nbytes=4 * 1024 * 1024 if want_prompt else TAIL_BYTES)
    out = {}
    for line in reversed(text.splitlines()):
        if not line.startswith("{"):
            continue
        # Cheap prefilter: json.loads on every line is the whole cost here, and
        # most lines carry none of these fields.
        if not ("Title" in line or "cwd" in line or "teamName" in line
                or (want_prompt and ("lastPrompt" in line or "gitBranch" in line
                                     or "cost-state" in line or '"model"' in line))):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        # Only trust these on an actual cost-state record. A bare substring match
        # hits message prose that merely mentions the field name.
        if rec.get("type") == "cost-state":
            for key in ("totalCostUSD", "totalLinesAdded", "totalLinesRemoved"):
                if key not in out and rec.get(key) is not None:
                    out[key] = rec[key]
        for key in ("customTitle", "aiTitle", "cwd", "gitBranch"):
            if key not in out and rec.get(key):
                out[key] = rec[key]
        # A teammate's own transcript carries teamName on ordinary records.
        # agentName by itself means nothing: every session writes it on
        # `type: agent-name` records as its display name.
        if rec.get("teamName") and rec.get("type") != "agent-name":
            out.setdefault("teamName", rec["teamName"])
            if rec.get("agentName"):
                out.setdefault("agentName", rec["agentName"])
        if want_prompt:
            if "lastPrompt" not in out and rec.get("lastPrompt"):
                out["lastPrompt"] = rec["lastPrompt"]
            mdl = (rec.get("message") or {}).get("model")
            if mdl and "model" not in out:
                out["model"] = mdl
        # In list mode stop as soon as the display fields are known. In preview
        # mode keep going: model, cost and branch sit further back than the
        # title, and stopping early silently omitted them.
        if not want_prompt and "customTitle" in out and "cwd" in out:
            break
    return out


def team_leads(root):
    """Map lead session id -> (team name, member count).

    Read only from ~/.claude/teams/<team>/config.json, which carries
    leadSessionId and a members array. That directory is removed when the lead's
    session ends, so this identifies CURRENT leads only.

    A ~/.claude/tasks/<id>/ directory is deliberately NOT used as evidence of
    leadership, though it looks like it should be. It is created for any session
    that has the Task tools, team or not, so treating a task list as proof of a
    team marks ordinary sessions as leads.
    """
    leads = {}
    for cfg in (root / "teams").glob("*/config.json"):
        try:
            data = json.loads(cfg.read_text("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        sid = data.get("leadSessionId")
        if sid:
            leads[sid] = (data.get("name") or cfg.parent.name,
                          len(data.get("members") or []))
    return leads


def decode_dir(project_dir):
    """Fallback cwd when no record carried one.

    LOSSY, and marked with "?" wherever used. The store encodes a path by
    replacing both "/" and "_" with "-", so the reverse is ambiguous:
    "-home-me-claude-projects" could be ~/claude_projects or
    ~/claude/projects and nothing in the name says which. Only empty sessions
    reach this path, and a directory that does not exist is never chdir'd into.
    """
    return "/" + project_dir.name.lstrip("-").replace("-", "/")


def head_facts(path, max_lines=25, max_bytes=512 * 1024):
    """(entrypoint, has_messages_in_head) from the START of a session.

    entrypoint distinguishes how the session was launched: "cli" is a session a
    person started, while "sdk-py" and "sdk-cli" are programmatic runs through
    the Agent SDK. On this store 81% of sessions are SDK runs, which is most of
    what makes an unfiltered list unusable.

    Bounded by LINE COUNT, not bytes. A single attachment record can be hundreds
    of kilobytes, so a byte window silently stops before the entrypoint record:
    one session here has a 434 KB first line with the entrypoint on line 3.
    """
    ep, seen = None, False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines, used = [], 0
            for _ in range(max_lines):
                line = fh.readline()
                if not line or used > max_bytes:
                    break
                used += len(line)
                lines.append(line)
    except OSError:
        return None, False
    for line in lines:
        if not line.startswith("{"):
            continue
        if not seen and ('"type":"assistant"' in line or '"type":"user"' in line
                         or '"type": "assistant"' in line or '"type": "user"' in line):
            seen = True
        if ep is None and '"entrypoint"' in line:
            try:
                ep = json.loads(line).get("entrypoint")
            except ValueError:
                pass
        if ep and seen:
            break
    return ep, seen


def block_text(content):
    """Plain text out of a message body, which is either a string or a list of
    typed blocks. Tool calls and tool results carry no prose and are skipped."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for blk in content:
        if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
            parts.append(str(blk["text"]).strip())
    return "\n".join(p for p in parts if p).strip()


def last_by_role(path, max_scan=6000):
    """Newest user prompt and newest assistant reply, as a dict.

    Both are wanted, not whichever came last: the prompt says what was asked and
    the reply says where it got to, and a session can end on either. Skips the
    machinery that is not conversation: meta records, tool results, attachments,
    transcript-only entries, and the local-command envelopes the CLI writes for
    slash commands.
    """
    out = {}
    text = tail_text(path, nbytes=2 * 1024 * 1024)
    for line in reversed(text.splitlines()[-max_scan:]):
        if len(out) == 2:
            break
        if not line.startswith("{"):
            continue
        if not any(t in line for t in ('"type":"assistant"', '"type":"user"',
                                       '"type": "assistant"', '"type": "user"')):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        role = rec.get("type")
        if role not in ("user", "assistant") or role in out:
            continue
        if rec.get("isMeta") or rec.get("isVisibleInTranscriptOnly"):
            continue
        body = block_text((rec.get("message") or {}).get("content"))
        if not body:
            continue
        if body.startswith("<") and body.endswith(">"):
            continue
        if body.startswith("<command-name>") or body.startswith("<local-command"):
            continue
        out[role] = body
    return out


def last_message(path, max_scan=6000):
    """Single answer: whichever of the two is newer."""
    got = last_by_role(path, max_scan)
    if "user" in got:
        return "user", got["user"]
    if "assistant" in got:
        return "assistant", got["assistant"]
    return None, ""


def has_conversation(path, size):
    """Whether a session holds any real message.

    Some sessions are opened and abandoned: they contain only metadata records
    and even their last-prompt entries carry no text, so they can never be
    identified or usefully resumed. Big files always have messages, so only the
    small ones are read.
    """
    if size > EMPTY_PROBE_BYTES:
        return True
    return bool(last_message(path)[1])


def is_scratch(cwd):
    """Sessions rooted in a temp directory.

    NOT subagents, despite looking disposable. Subagent transcripts live one
    level deeper, at {project}/{sessionId}/subagents/agent-*.jsonl, and are
    already outside this script's glob. These are ordinary sessions someone
    started in a scratch directory, so they are hidden as noise rather than
    excluded as machinery.
    """
    return cwd == "/tmp" or cwd.startswith("/tmp/")


def first_prompt(path, max_lines=400):
    """The opening user message, read from the HEAD of the file.

    A session's first prompt is usually the best one-line answer to "what was
    this?", and it is stable where the last message is whatever happened to be
    in flight when the session stopped.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(max_lines):
                line = fh.readline()
                if not line:
                    break
                if '"type":"user"' not in line and '"type": "user"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("isMeta") or rec.get("isVisibleInTranscriptOnly"):
                    continue
                body = block_text((rec.get("message") or {}).get("content"))
                if body and not body.startswith("<"):
                    return body
    except OSError:
        pass
    return ""


COST_MARK = b'"type":"cost-state"'


def last_line_with(path, marker, chunk=4 * 1024 * 1024):
    """(offset just past it, parsed record) of the LAST line containing marker.

    Reads backwards in chunks with no size limit, because a resumed session can
    leave its newest cost record more than 10 MB from the end of the file.
    """
    try:
        with path.open("rb") as fh:
            end = fh.seek(0, 2)
            pos, tail = end, b""
            while pos > 0:
                start = max(0, pos - chunk)
                fh.seek(start)
                buf = fh.read(pos - start) + tail
                i = buf.rfind(marker)
                if i >= 0:
                    a = buf.rfind(b"\n", 0, i) + 1
                    b = buf.find(b"\n", i)
                    b = len(buf) if b < 0 else b
                    try:
                        return start + b, json.loads(buf[a:b])
                    except ValueError:
                        return None, None
                # Keep a partial first line so a match split across chunks is found.
                cut = buf.find(b"\n")
                tail = buf[:cut] if cut >= 0 else buf
                pos = start
    except OSError:
        pass
    return None, None


def weighted_tokens(u):
    """Tokens weighted by their price relative to the input price.

    These ratios are Anthropic's published pricing structure: output is 5x the
    input price, cache reads 0.1x, 5-minute cache writes 1.25x and 1-hour cache
    writes 2x. The base price itself is never hard-coded; see model_rates().
    """
    cc = u.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens") or 0
    w5m = cc.get("ephemeral_5m_input_tokens")
    if w5m is None:
        w5m = (u.get("cache_creation_input_tokens") or 0) - w1h
    return ((u.get("input_tokens") or 0) + 5 * (u.get("output_tokens") or 0)
            + 0.1 * (u.get("cache_read_input_tokens") or 0) + 1.25 * w5m + 2 * w1h)


def usage_by_model(path, after=None, before=None):
    """Weighted tokens per model from replies timestamped in (after, before].

    Covers the session's own file AND its subagent transcripts, because
    subagent spend is billed to the parent and included in its cost records.
    One API response is written as several records sharing a message id, each
    repeating the same usage, so only the last record per id is counted.
    """
    seen = {}
    files = [path, *(path.parent / path.stem / "subagents").glob("*.jsonl")]
    for f in files:
        try:
            fh = f.open("rb")
        except OSError:
            continue
        with fh:
            for line in fh:
                if b'"usage"' not in line or b'"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = rec.get("timestamp") or ""
                if (after and ts <= after) or (before and ts > before):
                    continue
                msg = rec.get("message") or {}
                if msg.get("usage") and msg.get("model") and msg.get("id"):
                    seen[msg["id"]] = (msg["model"], weighted_tokens(msg["usage"]))
    out = {}
    for model, w in seen.values():
        out[model] = out.get(model, 0) + w
    return out


def time_before(path, offset, span=256 * 1024):
    """Timestamp of the last record before a byte offset: when a cost record,
    which carries no timestamp of its own, was written."""
    try:
        with path.open("rb") as fh:
            start = max(0, offset - span)
            fh.seek(start)
            buf = fh.read(offset - start)
    except OSError:
        return None
    for line in reversed(buf.splitlines()):
        i = line.find(b'"timestamp":"')
        if i >= 0:
            return line[i + 13:i + 37].decode("ascii", "replace").split('"')[0]
    return None


def model_rates(want, store, skip=None, max_files=200):
    """Price per weighted token for each model in `want`, measured from cost
    records that Claude Code itself wrote.

    No price table to go stale: a cost-state record carries tokens and dollars
    per model, so the rate falls out of dividing one by the other. Searches the
    most recent sessions until every wanted model is covered.
    """
    rates = {}
    files = sorted(store.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:max_files]:
        if not want - rates.keys():
            break
        if path == skip:
            continue
        _, rec = last_line_with(path, COST_MARK)
        for model, u in ((rec or {}).get("modelUsage") or {}).items():
            if model in want and model not in rates and u.get("costUSD"):
                w = (u.get("inputTokens", 0) + 5 * u.get("outputTokens", 0)
                     + 0.1 * u.get("cacheReadInputTokens", 0)
                     + 1.25 * u.get("cacheCreationInputTokens", 0))
                if w:
                    rates[model] = u["costUSD"] / w
    return rates


def session_cost(path):
    """(dollars, exact, lines_added, lines_removed) for a session.

    Claude Code writes a cost-state record only when a session process exits,
    and its totals carry across resumes. So the newest record is exact up to
    that exit, and anything after it (a session still running, or one that
    crashed) is not counted. That remainder is estimated from the usage on each
    reply, priced at rates measured from real cost records.
    """
    off, rec = last_line_with(path, COST_MARK)
    rec = rec or {}
    cost = rec.get("totalCostUSD") or 0.0
    when_written = time_before(path, off) if off else None
    after = usage_by_model(path, after=when_written)
    if not after:
        return cost, True, rec.get("totalLinesAdded"), rec.get("totalLinesRemoved")
    # Rate from this session's own record first, so its cache mix is matched.
    rates = {}
    if off:
        mine = usage_by_model(path, before=when_written)
        for model, u in (rec.get("modelUsage") or {}).items():
            if mine.get(model) and u.get("costUSD"):
                rates[model] = u["costUSD"] / mine[model]
    missing = set(after) - rates.keys()
    if missing:
        rates.update(model_rates(missing, path.parent.parent, skip=path))
    priced = [m for m in after if m in rates]
    if not priced:
        return (cost or None), not after, rec.get("totalLinesAdded"), rec.get("totalLinesRemoved")
    cost += sum(after[m] * rates[m] for m in priced)
    return cost, False, rec.get("totalLinesAdded"), rec.get("totalLinesRemoved")


def find_session(root, sid):
    for p in (root / "projects").glob(f"*/{sid}.jsonl"):
        return p
    return None


def collect(root, limit, here_only, deep, include_teams=True, include_scratch=False,
            include_empty=False, limit_counts_visible=False):
    """Walk the store newest-first.

    limit_counts_visible exists for the picker, which LOADS agent sessions so
    the toggle can reveal them without a rescan but HIDES them initially.
    Without it the limit is spent on rows nobody asked to see: with 81% of the
    store being SDK runs, a limit of 300 yielded about 8 visible rows.
    """
    files = sorted((root / "projects").glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    here = str(Path.cwd().resolve())
    leads = team_leads(root)
    rows = []
    counted = 0
    for path in files:
        if limit and counted >= limit:
            break
        # Cheap checks first. Testing the entrypoint before the tail scan avoids
        # reading 256 KB from each SDK session only to discard it.
        st = path.stat()
        sid = path.stem
        entrypoint, _ = head_facts(path)
        is_sdk = bool(entrypoint and entrypoint.startswith("sdk"))
        if not include_teams and is_sdk:
            continue
        if not include_empty and not has_conversation(path, st.st_size):
            continue

        got = scan(path, whole_file=deep)
        cwd = got.get("cwd")
        guessed = cwd is None
        cwd = cwd or decode_dir(path.parent)
        if here_only and cwd != here:
            continue
        team = got.get("teamName")
        if team:
            role, team_info = "team", team
        elif is_sdk:
            # Launched through the Agent SDK rather than by a person. Not a
            # teammate, which is why it never had an agent name to lose.
            role, team_info = "sdk", f"launched via {entrypoint}"
        elif sid in leads:
            role, team_info = "lead", f"{leads[sid][0]} ({leads[sid][1]} members)"
        else:
            role, team_info = "", ""
        # Filter here rather than after, so -n counts rows you actually see.
        if not include_teams and role == "team":
            continue
        if not include_scratch and is_scratch(cwd):
            continue
        if not limit_counts_visible or role not in ("team", "sdk"):
            counted += 1
        rows.append({
            "mtime": st.st_mtime,
            # A teammate has no title of its own, so its assigned agent name is
            # the only thing that identifies it.
            "name": (got.get("customTitle") or got.get("aiTitle")
                     or got.get("agentName") or "-"),
            "role": role,
            "team": team_info,
            "agent": got.get("agentName") or "",
            "cwd": cwd,
            "id": path.stem,
            "bytes": st.st_size,
            "named": bool(got.get("customTitle")),
            "guessed": guessed,
        })
    return rows, len(files)


def preview_text(root, sid, width=80):
    """The preview pane's contents, as a string.

    Returned rather than printed so the UI can render it; --preview prints it so
    the same output can be checked from a shell.
    """
    out = []
    path = find_session(root, sid)
    if not path:
        return "session file not found"
    got = scan(path, want_prompt=True)
    st = path.stat()
    name = (got.get("customTitle") or got.get("aiTitle")
            or got.get("agentName") or "(untitled)")
    out.append(f"{name}\n")
    if got.get("customTitle") and got.get("aiTitle"):
        out.append(f"  generated title  {got['aiTitle']}")
    out.append(f"  directory        {got.get('cwd') or decode_dir(path.parent)}")
    branch = got.get("gitBranch")
    if branch:
        out.append(f"  git branch       {branch}")
    out.append(f"  last active      "
               f"{time.strftime('%a %-d %b %Y, %-I:%M %p', time.localtime(st.st_mtime))}")
    model = got.get("model")
    if model:
        out.append(f"  model            {model}")
    cost, exact, added, removed = session_cost(path)
    if isinstance(cost, (int, float)) and cost > 0:
        out.append(f"  cost             ${cost:,.2f}" if exact else
                   f"  cost             ≈ ${cost:,.2f}  (estimated, see README FAQ)")
    if isinstance(added, int) and isinstance(removed, int) and (added or removed):
        out.append(f"  lines changed    +{added:,} / -{removed:,}")
    out.append(f"  size             {size_str(st.st_size)}")
    if got.get("teamName"):
        out.append(f"  agent team       teammate in {got['teamName']}")
        if got.get("agentName"):
            out.append(f"  agent name       {got['agentName']}")
    else:
        leads = team_leads(root)
        if sid in leads:
            nm, count = leads[sid]
            out.append(f"  agent team       LEAD of {nm}, {count} members")
    out.append(f"  id               {sid}\n")

    wrap = max(30, width - 4)

    def wrapped(text, limit=None):
        if limit and len(text) > limit:
            text = text[:limit].rstrip() + "…"
        for para in text.splitlines():
            if not para.strip():
                out.append("")
                continue
            line = ""
            for wd in para.split():
                if len(line) + len(wd) + 1 > wrap:
                    out.append("  " + line)
                    line = wd
                else:
                    line = f"{line} {wd}".strip()
            if line:
                out.append("  " + line)

    # Fixed order, always all three, so the pane has the same shape every time
    # and a missing piece is visible rather than silently absent.
    by_role = last_by_role(path)
    prompt = got.get("lastPrompt") or by_role.get("user")
    reply = by_role.get("assistant")
    opening = first_prompt(path)
    if opening and prompt and opening.strip() == str(prompt).strip():
        opening = ""  # a one-exchange session would otherwise print it twice

    for label, body, limit in (("last prompt", prompt, 700),
                               ("last msg from Claude", reply, 700),
                               ("started with", opening, 500)):
        out.append(f"▸ {label}\n")
        if body:
            wrapped(str(body), limit)
        else:
            out.append("  (no record)")
        out.append("")
    return "\n".join(out)



def resume_cmd(row, yolo):
    cmd = ["claude", "--resume", row["id"]]
    return cmd + [DANGER_FLAG] if yolo else cmd


def ids(root):
    """Every session id in the store, for --id prefix lookups."""
    return [p.stem for p in (root / "projects").glob("*/*.jsonl")]


def count(root):
    return len(list((root / "projects").glob("*/*.jsonl")))


"""Parse a bash command into the simple commands it would run, with their
words, assignments, redirects and structural context. Text only: nothing is
ever executed or expanded.

Parsing is tree-sitter-bash (pinned upstream, not forked). This module walks
the tree. It holds no policy: what a command MEANS (a launch, an unsafe
write) is for the caller to decide.

    result = parse('cd /w && make >log 2>&1')
    result.status                    "ok" | "syntax-error" | "too-big"
    for cmd in result.commands:      source order, substitutions included
        cmd.name, cmd.words, cmd.assignments, cmd.redirects,
        cmd.ancestors, cmd.op_before, cmd.op_after, cmd.background, cmd.negated

Known grammar limits (measured 2026-10-08 on 162,750 real commands):
- two heredocs opened on one line (`cat <<a; cat <<b`) are a syntax error to
  it (175 real commands); so is the `n<>file` redirect;
- `! { a; }` is misparsed, silently, as a command named `{`; parse() reports
  that as a syntax error (21 real commands);
- what the grammar gets wrong silently is reported as a syntax error: a line
  continuation inside a word (`r\\<newline>m`, which it splits in two), a
  substitution it leaves as text (any unescaped `$(` or backtick in text no
  node accounts for: see _unparsed), a backslash inside backticks, a NUL or
  an unquoted control character, and a reserved word where a command name goes;
- a space-indented heredoc terminator (bash rejects it) is accepted;
- parse time is quadratic or worse on many malformed shapes, and the grammar
  can't be cancelled, so a source over IN_PROCESS_BYTES, or with any non-ASCII
  character, is parsed in a child
  given up at DEADLINE; cheap counts (MAX_HEREDOCS, MAX_ARRAYS, MAX_BRACKETS,
  MAX_PIPES, MAX_BYTES) run first. All of these give "too-big", as does
  nesting deeper than MAX_DEPTH, since every command carries its chain, and a
  line of more than MAX_COMMANDS commands;
- `x=1 >f` (an assignment with a redirect) is a syntax error to it.
"""
import os
import pickle
import re
import subprocess
import sys
import threading
from pathlib import Path
from dataclasses import dataclass, replace

OK, SYNTAX_ERROR, TOO_BIG = "ok", "syntax-error", "too-big"
# Real max 78 KB. 1 MB of `a;` takes 0.6 s in the grammar alone (2026-10-09).
MAX_BYTES = 1024 * 1024
# 1,000 heredocs parse in 0.07 s, 40,000 in 56 s; real max 12. Counts every
# `<<` (here-strings, quoted text too), so it errs towards giving up early.
MAX_HEREDOCS = 256
# 8,000 unclosed nested `a=(` parse in 1.5 s, 50,000 in 46 s; real max 19.
MAX_ARRAYS = 1000
# 5,000 pipeline stages parse in 0.5 s, 40,000 in 51 s (2026-10-09); real max
# 849 `|`. Counts every `|` (`||`, quoted text too), so it errs towards giving up.
MAX_PIPES = 2000
# Unbalanced or repeated brackets send the grammar's error recovery quadratic:
# 80 KB of `))` takes 15 s, while 10,000 brackets of the worst shape take 1.5 s
# (2026-10-09). Real max 4,282.
MAX_BRACKETS = 10_000
# The grammar is quadratic on many malformed shapes, too many to cap one by one
# (40 KB of `a?` takes 7 s, 200 KB of `== ` 28 s), and it can't be cancelled:
# its progress callback segfaults (py-tree-sitter 0.25.2 and 0.26.0,
# 2026-10-09). So a source over IN_PROCESS_BYTES is parsed in a child
# interpreter and given up at DEADLINE seconds, as "too-big". The child also
# keeps a crash in the grammar (a SIGSEGV on U+10FFFF after `${`) out of the
# caller; it costs about 30 ms a parse.
IN_PROCESS_BYTES = 8 * 1024
DEADLINE = 3.0
ALWAYS_CHILD = os.environ.get("BASHTREE_ALWAYS_CHILD") == "1"   # a hook that must survive a crash in the grammar sets this
CHILD_MEMORY = 2 << 30                              # address space the parse child may use (a 1 MB input peaks near 0.6 GB)
# Every command carries its whole ancestor chain, so very deep nesting makes the
# result itself quadratic (5,000 chained && = 25M entries). Real max: 48.
MAX_DEPTH = 256
# Commands in one line: each is a full record. Real max: 95.
MAX_COMMANDS = 10_000
MAX_REPAIRS = 50                                    # newlines the grammar swallowed that we put right; more are refused

# Node types that are a word passed to a command.
_WORDS = {"word", "string", "raw_string", "ansi_c_string", "concatenation", "number",
          "simple_expansion", "expansion", "command_substitution", "translated_string",
          "process_substitution", "arithmetic_expansion", "brace_expression"}
_EXPANDING = {"simple_expansion", "expansion", "command_substitution", "process_substitution",
              "arithmetic_expansion", "brace_expression"}
_REDIRECTS = {"file_redirect", "heredoc_redirect", "herestring_redirect"}
_SEPARATORS = {";", "&", ";;", ";&", ";;&"}
# Statements a pipeline stage, list side or redirect can wrap.
_TRANSPARENT = {"redirected_statement", "negated_command", "pipeline"}

_LOCAL = threading.local()                          # a Parser isn't safe to share across threads


def _parser():
    if not hasattr(_LOCAL, "parser"):
        import tree_sitter_bash
        from tree_sitter import Language, Parser
        _LOCAL.parser = Parser(Language(tree_sitter_bash.language()))
    return _LOCAL.parser


# -- results ------------------------------------------------------------------
@dataclass(frozen=True)
class Word:
    text: str          # as the shell passes it on: quotes removed, escapes resolved
    raw: str           # as written
    literal: bool      # False if it holds an expansion, substitution, glob, brace or ~
    span: tuple        # (start byte, end byte)


@dataclass(frozen=True)
class Assignment:
    name: str
    raw: str           # the whole `NAME=value` as written
    value: str         # resolved like a word; "" when there is none (`export X`)
    value_raw: str
    has_substitution: bool
    append: bool       # `+=`
    span: tuple


@dataclass(frozen=True)
class Redirect:
    op: str            # > >> < &> &>> >| >& <& << <<- <<< >&- ...
    fd: str | None     # "2" in `2>f`, "{v}" in `{v}>f`; None when not written
    target: Word | None   # None for `>&-`; the delimiter for a heredoc
    heredoc_quoted: bool | None   # for << / <<-: body is literal (`<<'E'`)
    span: tuple
    words: tuple = ()  # words written after the target (`>f a b`: a, b). The shell
    #                    hands them to the command, and Command.words includes them


@dataclass(frozen=True)
class Ancestor:
    kind: str          # see KINDS
    span: tuple
    op: str | None = None          # kind "list": && or ||
    side: int | None = None        # kind "list": 0 left, 1 right
    stage: int | None = None       # kind "pipeline": this stage's index
    count: int | None = None       # kind "pipeline": number of stages
    redirects: tuple = ()          # "redirected", "function_body": apply to everything inside
    background: bool = False       # this construct is run with a trailing &
    negated: bool = False          # this construct carries a leading !


KINDS = ("list", "pipeline", "redirected", "if_condition", "elif_condition", "if_body",
         "while_condition",
         "loop_header", "loop_body", "case_word", "case_pattern", "case_body",
         "function_body", "subshell", "brace_group", "command_substitution",
         "heredoc_substitution", "process_substitution", "test_command", "arithmetic")


@dataclass(frozen=True)
class Command:
    kind: str          # simple | declaration (export/local/...) | unset | assignment |
    #                    redirect (a statement that is only redirects: `>f`, `[[ x ]] >f`)
    name: str | None   # resolved first word; the keyword for declaration/unset
    words: tuple       # Word, name included as words[0]
    assignments: tuple  # Assignment: prefix (`X=1 cmd`), declared, or standalone
    redirects: tuple   # Redirect on this command; a compound's are on its ancestor, and
    #                    a pipeline's trailing one is its last stage's (as in bash)
    ancestors: tuple   # Ancestor, root first
    op_before: str | None   # None ; \n & && || ;; (what joins it to the previous one)
    op_after: str | None
    background: bool   # a trailing & applies, directly or to anything enclosing it
    negated: bool      # a leading ! applies to it or its pipeline
    span: tuple

    def inside(self, *kinds):
        """True if any ancestor is one of `kinds`."""
        return any(a.kind in kinds for a in self.ancestors)


@dataclass(frozen=True)
class Result:
    status: str
    commands: tuple = ()
    reason: str = ""


# -- word decoding ----------------------------------------------------------------
def _unescape(raw, special):
    """Drop the backslash before any character in `special` ("" = any). A
    backslash-newline joins lines."""
    out, i = [], 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw) and (not special or raw[i + 1] in special):
            if raw[i + 1] != "\n":
                out.append(raw[i + 1])
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


# bash's $'...' escapes. \x and octal give BYTES (so \xc3\xa9 is one é),
# \u and \U give characters; \cX and anything unknown stay as written.
_ANSI_C = re.compile(r"\\(x\{[0-9A-Fa-f]*\}?|x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}|[0-7]{1,3}|c\\\\|c.|.)", re.S)
_ANSI_C_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
                  "e": "\x1b", "E": "\x1b", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c(text):
    out, pos = bytearray(), 0
    for m in _ANSI_C.finditer(text):
        out += text[pos:m.start()].encode("utf-8", "surrogatepass")
        pos, s = m.end(), m[1]
        if s[0] == "x" and len(s) > 1:
            out.append(int(s[1:].strip("{}") or "0", 16) & 0xFF)   # \x{H..}: the low byte
        elif s[0] in "01234567":
            out.append(int(s, 8) & 0xFF)
        elif s == "c\\\\":                                # \c\\ : control-\, both backslashes eaten
            out.append(0x1C)
        elif s[0] == "c" and len(s) == 2:              # \cX: control-X, \c@ a NUL
            out.append(0x7F if s[1] == "?" else ord(s[1]) & 0x1F)
        elif s[0] in "uU" and len(s) > 1 and int(s[1:], 16) <= 0x10FFFF:
            out += chr(int(s[1:], 16)).encode("utf-8", "surrogatepass")
        else:
            out += _ANSI_C_SIMPLE.get(s, m[0]).encode("utf-8", "surrogatepass")
    out += text[pos:].encode("utf-8", "surrogatepass")
    return out.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")   # bash ends the word at a NUL


def _raw(node, src):
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _text(node, src):
    """A word as the shell passes it on: quotes removed, escapes resolved.
    Expansions stay as written; nothing is evaluated."""
    raw = _raw(node, src)
    t = node.type
    if t == "raw_string":
        return raw[1:-1]
    if t == "ansi_c_string":
        return _ansi_c(raw[2:-1])
    if t in ("word", "$"):                           # a `$` token can be `\$`
        return _unescape(raw, "")
    if t == "string":                                # in "...", \ escapes only these
        # The whole text, not its children: a newline between two lines of
        # the string is a gap between child nodes, and joining them lost it.
        return _unescape(raw[1:-1], '$`"\\\n')
    if t == "translated_string":                     # $"...": a "..." string
        return _text(node.named_children[-1], src)
    if t == "concatenation":
        kids = node.children                         # `a$"b"`: the $ is the quote's
        return "".join(_text(c, src) for k, c in enumerate(kids)
                       if not (c.type == "$" and k + 1 < len(kids) and kids[k + 1].type == "string"))
    return raw


# An unescaped glob or brace character, or a leading ~, in an unquoted word.
_UNQUOTED_SPECIAL = re.compile(r"(?<!\\)(?:\\\\)*[*?\[{]|(?:^|[=:])~")


def _literal(node, src):
    """False if the word may change on expansion: a parameter, substitution,
    arithmetic, glob, brace or tilde. Errs towards False."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in _EXPANDING:
            return False
        if n.type == "word" and _UNQUOTED_SPECIAL.search(_raw(n, src)):
            return False
        if n.type not in ("string", "raw_string", "ansi_c_string"):
            stack.extend(n.children)
        elif n.type == "string":                     # "...": only expansions count
            stack.extend(c for c in n.children if c.type in _EXPANDING)
    return True


_EXP_AT = re.compile(r"\$[A-Za-z0-9_$!#?@*\-{(\[]|`|[<>]\(")


def _expands(raw):
    """Does bash expand something in this word text (outside single quotes, unescaped)?
    The grammar splits `$1`, `$$`, `$-` after a quote into pieces that each look literal."""
    i, n, dq = 0, len(raw), False
    while i < n:
        c = raw[i]
        if c == "\\":
            i += 2
        elif c == "'" and not dq:
            j = raw.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif c == "$" and raw[i + 1:i + 2] == "'" and not dq:
            j = i + 2
            while j < n and raw[j] != "'":
                j += 2 if raw[j] == "\\" else 1
            i = j + 1
        else:
            if c == '"':
                dq = not dq
            if _EXP_AT.match(raw, i) and not (dq and raw[i] in "<>"):
                return True
            i += 1
    return False


def _word(node, src):
    raw = _raw(node, src)
    return Word(_text(node, src), raw, _literal(node, src) and not _expands(raw),
                (node.start_byte, node.end_byte))


def _has_substitution(node):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("command_substitution", "process_substitution"):
            return True
        stack.extend(n.children)
    return False


def _assignment(node, src):
    name = node.child_by_field_name("name")
    value = node.child_by_field_name("value")
    op = next((c.type for c in node.children if c.type in ("=", "+=")), "=")
    return Assignment(_raw(name, src) if name else "", _raw(node, src),
                      _text(value, src) if value is not None else "",
                      _raw(value, src) if value is not None else "",
                      value is not None and _has_substitution(value), op == "+=",
                      (node.start_byte, node.end_byte))


def _runs(node):
    """The word nodes among a file_redirect's children, grouped into runs that
    touch. The grammar sometimes splits one word in two (`> $S/a-$w.txt` is
    `$S/a-$` and `w.txt`); separate words have a gap between them."""
    runs = []
    for c in node.children:
        if c.type in _WORDS:
            if runs and runs[-1][-1].end_byte == c.start_byte:
                runs[-1].append(c)
            else:
                runs.append([c])
    return runs


def _joined(nodes, src):
    if len(nodes) == 1:
        return _word(nodes[0], src)
    words = [_word(c, src) for c in nodes]
    span = (nodes[0].start_byte, nodes[-1].end_byte)
    return Word("".join(w.text for w in words), src[span[0]:span[1]].decode("utf-8", "replace"),
                all(w.literal for w in words) and not _expands(src[span[0]:span[1]].decode("utf-8", "replace")), span)


def _redirect(node, src):
    span = (node.start_byte, node.end_byte)
    if node.type == "heredoc_redirect":
        op = next((c.type for c in node.children if c.type in ("<<", "<<-")), "<<")
        start = next((c for c in node.children if c.type == "heredoc_start"), None)
        delim = _raw(start, src) if start else ""
        quoted = any(ch in delim for ch in "'\"\\")
        target = Word(_unescape(delim.replace("'", "").replace('"', ""), ""), delim, True,
                      (start.start_byte, start.end_byte) if start else span)
        fd = next((c for c in node.children if c.type == "file_descriptor"), None)
        return Redirect(op, _raw(fd, src) if fd else None, target, quoted, span)
    fd_node = node.child_by_field_name("descriptor")
    dest = node.child_by_field_name("destination")
    if node.type == "herestring_redirect":
        op = "<<<"
        runs = _runs(node)
        dest = next((c for c in node.children if c.is_named), None)
        if runs:
            return Redirect(op, _raw(fd_node, src) if fd_node else None, _joined(runs[0], src), None, span)
    else:
        op = next((c.type for c in node.children if not c.is_named), "")
    target, more = (_word(dest, src) if dest is not None else None), ()
    if node.type == "file_redirect" and dest is not None:
        runs = _runs(node)
        if op.endswith("&-"):                        # `>&- x`: x is an argument, not a target
            target, more = None, tuple(_joined(r, src) for r in runs)
        else:
            target, more = _joined(runs[0], src), tuple(_joined(r, src) for r in runs[1:])
    return Redirect(op, _raw(fd_node, src) if fd_node else None, target, None, span, more)


def _with_dashes(reds, lo, src):
    """A lone `-` word between the command and a redirect (`tar xf - 2>f`,
    `python3 - <<E`) is in no node of the tree. Give it back as a word of the
    redirect that follows it, which Command.words takes in order."""
    out = []
    for r in reds:
        gap = src[lo:r.span[0]]
        if _DASH.fullmatch(gap):
            at = lo + gap.index(b"-")
            r = replace(r, words=(Word("-", "-", True, (at, at + 1)),) + r.words)
        lo = r.span[1]
        out.append(r)
    return tuple(out)


def _merge_assignment(a, w):
    """`X=$A/$b.c`: the grammar ends the assignment early; the touching word is
    the rest of its value."""
    return replace(a, raw=a.raw + w.raw, value=a.value + w.text, value_raw=a.value_raw + w.raw,
                   has_substitution=a.has_substitution or bool(_SUBST_TEXT.search(w.raw)),
                   span=(a.span[0], w.span[1]))


def _redirects_of(nodes, src):
    """Redirects among `nodes`, in source order. The grammar nests redirects
    that follow a heredoc (`cat <<E >f`) inside the heredoc's node."""
    out, stack = [], [c for c in nodes if c.type in _REDIRECTS]
    while stack:
        r = stack.pop()
        out.append(_redirect(r, src))
        if r.type == "heredoc_redirect":
            stack.extend(c for c in r.children if c.type in _REDIRECTS)
    return _in_order(tuple(out))


def _in_order(reds):
    return tuple(sorted(reds, key=lambda r: r.span[0]))


# What bash accepts as an assignment's name. The grammar is looser: it takes
# `1=2 cmd` as an assignment, where bash runs a command named `1=2`.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[.*\])?\Z", re.S)
_BLANK = re.compile(rb"(?:[ \t\n]|\\\n)*")                  # what may lie between a node's children
_DASH = re.compile(rb"(?:[ \t\n]|\\\n)*-(?:[ \t\n]|\\\n)*")  # a lone `-` word the grammar leaves out
_SUBST_TEXT = re.compile(r"\$\(|`|[<>]\(")


# -- structure --------------------------------------------------------------------
# One top-down walk carries each node's context down to it, so the cost is
# linear in the tree and depth costs list entries, never stack frames.
# (Climbing back up from every command was quadratic: 500 nested `$(` took
# 25 s, and a 1,000-long && chain overflowed the recursion limit.)

# Statements whose redirects apply to everything inside them.
_COMPOUND = {"subshell", "compound_statement", "if_statement", "while_statement", "for_statement",
             "c_style_for_statement", "case_statement"}
_FD_VAR = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}\Z")
_FD_NUM = re.compile(r"[0-9]+\Z")                    # ASCII: `²>f` is a word, then `>f`


def _separator_ops(kids, src):
    """{index: (op before, op after)} for each child among its siblings: a
    separator token, a newline in the gap, or None. Comments are skipped. One
    linear pass: walking sibling links per child was cubic on comment runs."""
    real = [i for i, c in enumerate(kids) if c.type != "comment"]
    out = {}
    for k, i in enumerate(real):
        node, ops = kids[i], []
        for j in (real[k - 1] if k else None, real[k + 1] if k + 1 < len(real) else None):
            if j is None:
                ops.append(None)
                continue
            sib = kids[j]
            if not sib.is_named and sib.type in _SEPARATORS:
                ops.append(sib.type)
            else:
                lo, hi = (sib.end_byte, node.start_byte) if j < i else (node.end_byte, sib.start_byte)
                ops.append("\n" if b"\n" in src[lo:hi] else None)
        out[i] = tuple(ops)
    return out


class _State:
    """A node's context: what encloses it, and its place in its statement list."""
    __slots__ = ("anc", "ops", "bg", "el_bg", "neg", "reds", "ptype")

    def __init__(self, anc, ops, bg, el_bg, neg, reds, ptype):
        self.anc, self.ops, self.bg, self.el_bg = anc, ops, bg, el_bg
        self.neg, self.reds, self.ptype = neg, reds, ptype


def _command(node, kids, src, st):
    kind = {"command": "simple", "declaration_command": "declaration",
            "unset_command": "unset"}.get(node.type, "assignment")
    words, assigns, reds = [], [], []
    if node.type in ("variable_assignments", "variable_assignment"):
        parts = node.named_children if node.type == "variable_assignments" else [node]
        for c in parts:
            if c.type != "variable_assignment":
                continue
            a = _assignment(c, src)
            if words or not _NAME.match(a.name):
                words.append(_word(c, src))           # not an assignment to bash: a word
            else:
                assigns.append(a)
        if words:
            kind = "simple"
    else:
        for c in kids:
            if (c.type == "variable_assignment" and kind == "simple"
                    and (words or not _NAME.match(_assignment(c, src).name))):
                words.append(_word(c, src))           # `1=2 cmd`: the command is `1=2`
            elif c.type == "variable_assignment":
                assigns.append(_assignment(c, src))
            elif c.type in ("command_name", *_WORDS, "variable_name", "$"):
                w = _word(c.children[0] if c.type == "command_name" and c.children else c, src)
                if assigns and not words and assigns[-1].span[1] == c.start_byte:
                    assigns[-1] = _merge_assignment(assigns[-1], w)   # a value the grammar cut short
                else:
                    words.append(w)                   # a lone `$` is passed on as `$`
            elif c.type in _REDIRECTS:
                reds += _redirects_of([c], src)
            elif not c.is_named and kind in ("declaration", "unset") and not words:
                words.append(_word(c, src))          # the keyword: export, local, unset ...
    reds = list(_in_order(tuple(st.reds) + tuple(reds)))
    if kind in ("simple", "declaration", "unset"):
        words = sorted(words + [w for r in reds for w in r.words], key=lambda w: w.span[0])
    # `{v}>f` and `0>f`: the grammar gives the fd as a word right before the
    # redirect. It is the redirect's fd, not an argument.
    starts = {r.span[0]: k for k, r in enumerate(reds) if r.fd is None and not r.op.startswith("&>")}
    kept = []
    for w in words:
        k = starts.get(w.span[1])
        if k is not None and (_FD_VAR.match(w.raw) or (_FD_NUM.match(w.raw) and int(w.raw) <= 2147483647)):
            reds[k] = replace(reds[k], fd=w.raw)
        elif kept and w.raw[:1] == '"' and kept[-1].span[1] == w.span[0] and re.search(r"(?<!\\)(?:\\\\)*\$\Z", kept[-1].raw):
            p = kept[-1]                              # $"..." : the $ is the quote's, as in `-q$"x"`
            kept[-1] = Word(p.text[:-1] + w.text, p.raw + w.raw, w.literal and p.literal and not _expands(p.raw + w.raw), (p.span[0], w.span[1]))
        elif kept and kept[-1].span[1] == w.span[0]:
            # Touching words are one word; the grammar splits some (`$a/$b.c`, a string then `\;`).
            p = kept[-1]
            kept[-1] = Word(p.text + w.text, p.raw + w.raw, p.literal and w.literal and not _expands(p.raw + w.raw), (p.span[0], w.span[1]))
        else:
            kept.append(w)
    words = kept
    if kind == "simple" and not words and assigns:
        kind = "assignment"                          # `f=$C/$n.md`: the grammar's "command" is only assignments
    return Command(kind, words[0].text if words else None, tuple(words), tuple(assigns),
                   tuple(reds), st.anc, st.ops[0], st.ops[1], st.bg, st.neg,
                   (node.start_byte, node.end_byte))


def _redirect_only(node, st, reds):
    """A statement that is only redirects (`>f`, `[[ x ]] >f`): it still
    creates or truncates the target."""
    return Command("redirect", None, (), (), tuple(reds), st.anc, st.ops[0], st.ops[1],
                   st.bg, st.neg, (node.start_byte, node.end_byte))


def _is_command(n, ptype):
    if n.type in ("command", "declaration_command", "unset_command", "variable_assignments"):
        return True
    # A standalone `X=1` is its own statement; inside a command or a
    # declaration it is a prefix, reported with that command.
    return n.type == "variable_assignment" and ptype not in (
        "command", "declaration_command", "variable_assignments")


def _entry(c, n, kids, st, extra):
    """The ancestor entry `n` contributes for its child `c`, or None."""
    t, span = n.type, (n.start_byte, n.end_byte)
    flags = dict(background=st.el_bg, negated=st.neg)
    if t in ("if_statement", "elif_clause"):
        if c.type in ("elif_clause", "else_clause"):
            return None
        before_then = extra["index"] < extra["then"]
        if t == "elif_clause" and before_then:
            return Ancestor("elif_condition", span)  # runs only if earlier conditions failed
        return Ancestor("if_condition" if before_then else "if_body", span)
    if t == "else_clause":
        return Ancestor("if_body", span)
    if t == "while_statement":
        return None if c.type == "do_group" else Ancestor("while_condition", span)
    if t in ("for_statement", "c_style_for_statement"):
        if c.type == "do_group":
            return None
        if c.type == "compound_statement" and c.id == (n.child_by_field_name("body") or c).id:
            return Ancestor("loop_body", span)       # `for ((;;)) { a; }`
        return Ancestor("loop_header", span)
    if t == "do_group":
        return Ancestor("loop_body", span)
    if t == "case_statement":
        return None if c.type == "case_item" else Ancestor("case_word", span)
    if t == "case_item":
        return Ancestor("case_body" if extra["index"] > extra["paren"] else "case_pattern", span)
    if t == "function_definition":
        return Ancestor("function_body", span, redirects=extra["fn_reds"])
    if t == "compound_statement":
        if kids and kids[0].type == "((":
            return Ancestor("arithmetic", span)
        return None if st.ptype == "function_definition" else Ancestor("brace_group", span, **flags)
    if t == "subshell":
        return None if st.ptype == "function_definition" else Ancestor("subshell", span, **flags)
    if t == "command_substitution":
        if st.ptype == "heredoc_body" and extra["src"][n.start_byte:n.start_byte + 3] == b"$((":
            return Ancestor("arithmetic", span)      # the grammar reads `$((1+2))` there as `$( (1+2) )`
        return Ancestor("heredoc_substitution" if st.ptype == "heredoc_body" else "command_substitution", span)
    if t == "process_substitution":
        return Ancestor("process_substitution", span)
    if t == "test_command":
        return Ancestor("test_command", span)
    if t in ("arithmetic_expansion", "arithmetic_command"):
        return Ancestor("arithmetic", span)
    return None


def _walk(root, src):
    out = []
    stack = [(root, _State((), (None, None), False, False, False, (), None))]
    while stack:
        n, st = stack.pop()
        t = n.type
        kids = n.children                            # a fresh list per access: read once
        if _is_command(n, st.ptype):
            out.append(_command(n, kids, src, st))
            if len(out) > MAX_COMMANDS:
                return None
            reds = ()
        else:
            reds = st.reds
        own = ()
        if t in _REDIRECTS and n.parent is not None and n.parent.type == "command_substitution":
            out.append(_redirect_only(n, st, _redirects_of([n], src)))      # `$(>f)`
        if t == "redirected_statement":
            own = _redirects_of(kids, src)
            if n.child_by_field_name("body") is not None:
                own = _with_dashes(own, n.child_by_field_name("body").end_byte, src)
            if n.child_by_field_name("body") is None:
                out.append(_redirect_only(n, st, st.reds + own))
                own = reds = ()
        anc = st.anc
        # The grammar gives `(( expr ))` as a compound_statement opening with `((`.
        arith = t == "compound_statement" and bool(kids) and kids[0].type == "(("
        if reds and t in _COMPOUND and not arith:   # `{ ...; } >f`, `done >f`, `esac >f`
            anc = anc + (Ancestor("redirected", (n.start_byte, n.end_byte),
                                  redirects=_in_order(reds), background=st.el_bg, negated=st.neg),)
            reds = ()
        elif reds and (t in ("test_command", "arithmetic_command") or arith):
            out.append(_redirect_only(n, st, reds))
            reds = ()
        named = [c for c in kids if c.is_named and c.type != "comment"]
        extra = {"src": src}
        if t in ("if_statement", "elif_clause"):
            extra["then"] = next((i for i, c in enumerate(kids) if c.type == "then"), len(kids))
        if t == "case_item":
            extra["paren"] = next((i for i, c in enumerate(kids) if c.type == ")"), len(kids))
        if t == "function_definition":
            extra["fn_reds"] = _in_order(reds + _redirects_of(kids, src))
            reds = ()
        see_through = t in ("list", "pipeline", "negated_command", "redirected_statement")
        if see_through:
            pipe_neg = t == "pipeline" and bool(named) and named[0].type == "negated_command"
            list_op = next((c.type for c in kids if c.type in ("&&", "||")), None) if t == "list" else None
            body = n.child_by_field_name("body") if t == "redirected_statement" else None
            last = named[-1] if named else None
        else:
            ops_of = _separator_ops(kids, src)
        children, stage = [], 0
        for i, c in enumerate(kids):
            if c.type == "comment":
                continue                             # holds no commands
            extra["index"] = i
            if see_through:
                # The same statement, seen through: it keeps the parent's place.
                ops, el_bg, neg = st.ops, st.el_bg, st.neg or t == "negated_command" or pipe_neg
                c_anc, c_reds = anc, ()
                if t == "list" and c.is_named:
                    left = c.id == named[0].id
                    ops = (st.ops[0], list_op) if left else (list_op, st.ops[1])
                    c_anc = anc + (Ancestor("list", (n.start_byte, n.end_byte), op=list_op,
                                            side=0 if left else 1, background=st.el_bg,
                                            negated=st.neg),)
                    if not left:
                        c_reds = reds                # `a && b >f`: the redirect is b's
                elif t == "pipeline" and c.is_named:
                    c_anc = anc + (Ancestor("pipeline", (n.start_byte, n.end_byte), stage=stage,
                                            count=len(named), background=st.el_bg, negated=neg),)
                    if c.id == last.id:
                        c_reds = reds                # `a | b >f`: the redirect is b's
                    stage += 1
                elif t == "negated_command" and c.is_named:
                    c_reds = reds
                elif t == "redirected_statement" and body is not None and c.id == body.id:
                    c_reds = reds + own
                bg = st.bg
            else:
                ops = ops_of[i]
                el_bg = ops[1] == "&"
                bg, neg, c_reds = st.bg or el_bg, False, ()
                e = _entry(c, n, kids, st, extra)
                c_anc = anc + (e,) if e is not None else anc
            if len(c_anc) > MAX_DEPTH:
                return None
            children.append((c, _State(c_anc, ops, bg, el_bg, neg, c_reds, t)))
        stack.extend(reversed(children))
    out.sort(key=lambda c: c.span[0])
    return out


_RESERVED = {"{", "}", "then", "do", "done", "fi", "esac", "elif", "else"}
# An unescaped substitution opener. `<(` and `>(` open one only unquoted.
_CODE_MARK = re.compile(rb"(?<!\\)(?:\\\\)*(?:`|\$\(|[<>]\()")
_QUOTED_MARK = re.compile(rb"(?<!\\)(?:\\\\)*(?:`|\$\()")
# Nodes whose bytes are data: a line continuation or CR inside them is no word break.
_OPAQUE = {"string", "string_content", "raw_string", "ansi_c_string", "comment", "heredoc_body",
           "heredoc_content", "translated_string"}
_CONTROL = (b"\r", b"\x0b", b"\x0c", "﻿".encode())   # bash keeps these in a word


def _unparsed(root, src):
    """Why the tree misses what bash would run, though it has no error node;
    None if it doesn't. Linear: one pass over the nodes, one over the text.

    The grammar leaves some substitutions as plain text (`${x%$(a)}`,
    `${x:-a `b`}`, a backtick after `$x` in a heredoc). Rather than list those
    shapes, every byte no child node covers is checked: an unescaped `$(` or
    backtick there is a substitution the tree doesn't show. Each node carries
    its quoting: "code", "dq" (inside "..." or an unquoted heredoc), "dqexp"
    (inside a `${ }` that is inside one, where single quotes don't quote), or
    "data" (single quotes, a quoted heredoc, a comment), which isn't checked."""
    stack = [(root, "code", False)]               # node, quoting, inside a `${ }`
    bodies = []          # unquoted heredoc bodies: bash joins their continuations, the grammar doesn't
    while stack:
        n, ctx, inexp = stack.pop()
        kids = n.children
        t = n.type
        if t == "command_substitution" and src[n.start_byte:n.start_byte + 1] == b"`":
            # Inside backticks bash removes a backslash before $ ` \ first,
            # so `\`` nests and `\$` expands: the grammar sees neither.
            if b"\\" in src[n.start_byte:n.end_byte]:
                return "a backslash inside backticks"
            if b"`" in src[n.start_byte + 1:n.end_byte - 1]:
                return "a backtick inside backticks"
        if t in ("file_redirect", "herestring_redirect", "heredoc_redirect"):
            fdn = n.child_by_field_name("descriptor") or next((c for c in kids if c.type == "file_descriptor"), None)
            if fdn is not None:                      # bash takes only digits up to INT_MAX or {ASCII name}
                ft = src[fdn.start_byte:fdn.end_byte]
                if not ((ft.isdigit() and ft.isascii() and int(ft) <= 2147483647)
                        or re.fullmatch(rb"\{[A-Za-z_][A-Za-z0-9_]*\}", ft)):
                    return "a file descriptor bash would not take as one"
        if t == "variable_assignment":
            head = re.split(rb"\]\+?=", src[n.start_byte:n.end_byte], 1)[0]
            if b"[" in head and re.search(rb"\[.*(\$\(|`)", head, re.S) and len(head) < n.end_byte - n.start_byte:
                return "a substitution in an assignment subscript"
        if t == "array" and re.search(rb"\[[^\]]*(\$\(|`)[^\]]*\]\+?=", src[n.start_byte:n.end_byte]):
            return "a substitution in an array subscript"
        if t == "variable_assignment" and any(a.end_byte != b.start_byte for a, b in zip(kids, kids[1:])):
            return "an assignment whose value is across white space"
        if t == "heredoc_start" and re.search(rb"[|&;()<> \t]", re.sub(rb"'[^']*'|\"(?:[^\"\\]|\\.)*\"|\\.", b"", src[n.start_byte:n.end_byte])):
            return "a heredoc delimiter holding a shell operator"
        if t == "heredoc_redirect":
            for a, b in zip(kids, kids[1:]):             # the command line part, up to the body
                if a.type in ("heredoc_body", "heredoc_end"):
                    break
                gap = src[a.end_byte:b.start_byte]
                if b.type in ("heredoc_body", "heredoc_end") and gap.endswith(b"\n"):
                    gap = gap[:-1]                         # the line break before the body
                if not _BLANK.fullmatch(gap):
                    return "text between the parts of a heredoc redirect"
        if t == "heredoc_redirect" and any(c.type in _WORDS for c in kids):
            # Words the grammar hangs on the heredoc: the command's arguments
            # after the delimiter (`cat <<E file`, 1 of 162,750 real commands),
            # or its body misread as words when a line opens with `\\``.
            return "words in a heredoc redirect"
        if t == "file_redirect":
            op = next((c.type for c in kids if not c.is_named), "")
            runs = _runs(n)
            first = src[runs[0][0].start_byte:runs[0][-1].end_byte] if runs else b""
            if op in (">&", "<&") and first.startswith(b"-"):
                return "a redirect target bash reads as a `-`"
            if op == ">&" and not any(c.type == "file_descriptor" for c in kids) \
                    and not re.fullmatch(rb"[0-9]+-?", first):
                return "a >& target, which bash expands twice"
        if t == "file_redirect" and len(runs) > (0 if op.endswith("&-") else 1):
            # `rm >f -rf x`: the grammar hangs the later words on the redirect.
            # Command.words takes them back (a pipeline's or list's redirect is
            # its last command's, so are they). Bash refuses `{ a; } >f g`. Words on
            # a later line are another command: _swallowed_newline puts those right.
            body = n.parent.children[0] if n.parent.type == "redirected_statement" else n.parent
            while body.type in ("pipeline", "list", "negated_command"):
                body = [c for c in body.named_children if c.type != "comment"][-1]   # the last stage
            if body.type not in ("command", "unset_command"):      # not `export >f A=$(x)`: they would be words, not assignments
                return "words after a redirect on something that isn't a command"
        if t in ("translated_string", "simple_expansion") and len(kids) > 1 and kids[0].end_byte != kids[1].start_byte:
            return "a $ joined to a string across white space"
        if t == "concatenation" and not inexp and any(a.end_byte != b.start_byte for a, b in zip(kids, kids[1:])):
            return "a word the grammar built across white space"
        if t == "word" and not kids and ctx == "code" and not inexp \
                and re.search(rb"(?<!\\)(?:\\\\)*[ \t\n]", src[n.start_byte:n.end_byte]):
            return "a word with white space in it"
        for a, b in zip(kids, kids[1:]):
            if b.start_byte != a.end_byte or not b.is_named:
                continue
            if a.type == "variable_assignment" and b.type not in _REDIRECTS and not (
                    t in ("command", "declaration_command") and b.type in ("command_name", *_WORDS, "variable_name")):
                return "an assignment glued to what follows"
            if a.type in _STATEMENTS and b.type in _STATEMENTS:
                return "two statements with nothing between them"
            if a.type in _REDIRECTS and b.type in (*_WORDS, "command_name"):
                return "a word glued to a redirect"
        for a, b in zip(kids, kids[1:]):
            if a.type in _REDIRECTS and b.type == "$" and a.end_byte == b.start_byte:
                return "a $ glued to a redirect"
        if t in ("command", "declaration_command", "unset_command", "redirected_statement") and len(kids) > 1:
            for a, b in zip(kids, kids[1:]):         # a command's pieces share one line (or a continuation)
                if re.search(rb"(?<!\\)\n", src[a.end_byte:b.start_byte]):
                    return "the next line was taken for part of this command"
        if t in ("==", "=~") and n.parent.type in ("command", "declaration_command", "unset_command"):
            return "a [[ operator outside [[ ]]"     # dropped, and it swallows the newline after it
        if t == "heredoc_body" and not _quoted_heredoc(n, src):
            end = next((c.end_byte for c in n.parent.children if c.type == "heredoc_end"), n.end_byte)
            bodies.append((n.start_byte, end))
        if t == "heredoc_end":
            # bash ends a heredoc at a line that is exactly the delimiter
            # (leading tabs allowed for <<-); the grammar stops at `E; touch f`.
            lead = src[src.rfind(b"\n", 0, n.start_byte) + 1:n.start_byte]
            if any(c.type == "<<-" for c in n.parent.children):
                lead = lead.strip(b"\t")
            if lead or src[n.end_byte:n.end_byte + 1] not in (b"", b"\n"):
                return "a heredoc terminator that is not alone on its line"
        if t in ("{", "}") and not n.is_named and n.parent.type == "compound_statement":
            # `{` and `}` are reserved words only when set apart: `{qfv}<f cmd` and `{cmd;}x` are plain words.
            nxt = src[n.end_byte:n.end_byte + 1]
            if nxt and nxt not in (b" \t\n" if t == "{" else b" \t\n;&|)<>"):
                return "a brace glued to the word next to it"
        if t in (";;", ";&", ";;&") and not n.is_named and n.parent.type != "case_item":
            return "a case terminator outside a case"
        if t == "case_statement" and len(kids) > 2 and kids[-1].type == "esac" and kids[-2].type != "in":
            prev = kids[-2]
            if not (prev.type == "case_item" and prev.children[-1].type in (";", "&", ";;", ";&", ";;&")) \
                    and not re.search(rb"[\n;&]", src[(prev.children[-1] if prev.children else prev).end_byte:kids[-1].start_byte]):
                return "an esac that is an argument, not the end of the case"
        if n.is_named:
            mark = _CODE_MARK if ctx == "code" else _QUOTED_MARK
            pos = n.start_byte
            for c in kids + [None]:
                gap = src[pos:c.start_byte if c else n.end_byte]
                if mark.search(gap):
                    return "a substitution the grammar left as text"
                if kids and ctx == "code" and not inexp and t not in _GAP_DATA and not _BLANK.fullmatch(gap) \
                        and not (t in _DASH_PARENTS and _DASH.fullmatch(gap)):
                    return "text the grammar did not account for"
                pos = c.end_byte if c else pos
        for c in kids:
            ct = c.type
            if ct in ("raw_string", "ansi_c_string") and re.search(rb"\$\(|`", src[c.start_byte:c.end_byte]):
                up = c.parent
                while up is not None:
                    if up.type in ("arithmetic_expansion", "arithmetic_command", "test_command", "subscript") or (
                            up.type == "compound_statement" and up.children and up.children[0].type == "(("):
                        return "a quoted substitution that arithmetic or a subscript would run"
                    up = up.parent
            if ct in _OPENERS and not src.startswith(_OPENERS[ct], c.start_byte):
                return "a quoted string the grammar started before its opening quote"
            if ct == "ansi_c_string" and src[c.start_byte:c.start_byte + 2] == b"$'":
                i, raw = 2, src[c.start_byte:c.end_byte]
                while i < len(raw) and raw[i] != 0x27:
                    i += 2 if raw[i] == 0x5C else 1
                if i != len(raw) - 1:
                    return "an ANSI-C string the grammar ended in the wrong place"
            if ct in ("command_substitution", "process_substitution", "arithmetic_expansion"):
                cc = "code"                          # a new shell context, even inside "..."
            elif ct in ("string", "translated_string"):
                cc = "dq"
            elif ct == "expansion":
                cc = "dqexp" if ctx in ("dq", "dqexp") else ctx
            elif ct in ("raw_string", "ansi_c_string"):
                cc = "dqexp" if ctx == "dqexp" else "data"
            elif ct == "comment" or (ct == "heredoc_body" and _quoted_heredoc(c, src)):
                if ct == "heredoc_body" and any(g.type in ("command_substitution", "process_substitution")
                                                for g in c.children):
                    return "a quoted heredoc the grammar parsed as code"
                cc = "data"
            elif ct == "heredoc_body":
                cc = "dq"
            else:
                cc = ctx
            if cc != "data":
                stack.append((c, cc, inexp or ct == "expansion"))
    if b"\x00" in src:
        return "a NUL byte"
    for mark in _CONTROL:
        p = src.find(mark)
        while p != -1:
            if root.descendant_for_byte_range(p, p + len(mark)).type not in _OPAQUE:
                return "a control character bash keeps in a word"
            p = src.find(mark, p + len(mark))
    p = src.find(b"\\\n")
    while p != -1:
        j = p
        while j >= 0 and src[j] == 0x5C:
            j -= 1
        if (p - j) % 2:                              # `\\` then a newline isn't one
            if any(a <= p < b for a, b in bodies):
                return "a line continuation inside a heredoc body"
            before, after = src[p - 1:p], src[p + 2:p + 3]
            k, esc = p - 2, False
            while k >= 0 and src[k] == 0x5C:             # `\(` then a continuation: the `(` is a letter
                esc, k = not esc, k - 1
            if p and before == b"\n" and root.descendant_for_byte_range(p, p + 2).type not in _OPAQUE:
                return "a line continuation at the start of a line"   # the grammar drops the newline before it
            in_word = (before and after and (esc or before not in b" \t\n;&|(<>") and after not in b" \t\n;&|)<>")
            joins = (before == b"$" or (before == b"(" and after == b"(")                                # `"$\<nl>(a)"` is `$(a)`
                     or (after in (b"<", b">") and (before.isdigit() or before == b"&")))   # `2\<nl>>f`
            if joins or (in_word and root.descendant_for_byte_range(p, p + 2).type not in _OPAQUE):
                return "a line continuation inside a word"
        p = src.find(b"\\\n", p + 2)
    return None


_OPENERS = {"raw_string": b"'", "ansi_c_string": b"$'", "string": b'"', "translated_string": b'$"'}
_STATEMENTS = {"command", "variable_assignment", "variable_assignments", "declaration_command", "unset_command"}
# Nodes whose children are not all of their text: data between them.
_GAP_DATA = {"heredoc_body", "string", "translated_string", "raw_string", "ansi_c_string", "comment",
             "string_content", "heredoc_redirect"}
_DASH_PARENTS = {"redirected_statement"}


def _swallowed_newline(root, src):
    """Byte offset of a newline the grammar took for white space inside a
    command, or None. After a pipeline, `a | b` newline `c 2>&1` is parsed as
    one command `b c`, a 2.4% misparse in real use; the fix is to make the
    newline a `;`, which keeps every offset. Only a gap of white space and
    that one newline qualifies (a comment would swallow the `;`)."""
    stack = [root]
    while stack:
        n = stack.pop()
        kids = n.children
        stack.extend(kids)
        if n.type in ("word", "concatenation", "command_name") and src[n.start_byte:n.start_byte + 1] == b"\n" \
                and not (n.prev_sibling is not None and n.prev_sibling.type == "comment"):   # a `;` there would be comment
            up = n.parent                            # `\rm` opening a line: the newline went into the word
            while up is not None and up.type != "heredoc_redirect":
                up = up.parent
            if up is None:
                return n.start_byte
        if n.type in ("command", "declaration_command", "unset_command", "redirected_statement"):
            pairs = zip(kids, kids[1:])
        elif n.type == "file_redirect":
            pairs = zip([c for c in kids if c.type in _WORDS], [c for c in kids if c.type in _WORDS][1:])
        else:
            continue
        for a, b in pairs:
            gap = src[a.end_byte:b.start_byte]
            if a.type != "comment" and b.type != "comment" and re.fullmatch(rb"[ \t]*\n[ \t]*", gap):
                return a.end_byte + gap.index(b"\n")
    return None


def _quoted_heredoc(body, src):
    start = next((c for c in body.parent.children if c.type == "heredoc_start"), None)
    return start is not None and any(ch in src[start.start_byte:start.end_byte] for ch in b"'\"\\")


def parse(text):
    """A Result. Commands come in source order, those inside substitutions
    included, each with its context. Status other than "ok": no commands."""
    if not isinstance(text, str):
        raise TypeError("parse() takes a str")
    if text.count("<<") > MAX_HEREDOCS:
        return Result(TOO_BIG, reason=f"over {MAX_HEREDOCS} `<<`")
    if text.count("=(") > MAX_ARRAYS:
        return Result(TOO_BIG, reason=f"over {MAX_ARRAYS} `=(`")
    if text.count("|") > MAX_PIPES:
        return Result(TOO_BIG, reason=f"over {MAX_PIPES} `|`")
    if sum(map(text.count, "()[]{}")) > MAX_BRACKETS:
        return Result(TOO_BIG, reason=f"over {MAX_BRACKETS} brackets")
    src = text.encode("utf-8", "surrogatepass")
    if len(src) > MAX_BYTES:
        return Result(TOO_BIG, reason=f"over {MAX_BYTES} bytes")
    # Non-ASCII text goes to the child too: py-tree-sitter 0.26.0 segfaults in the scanner on
    # `${` followed by U+F0000 and up (found 2026-10-09), and a crash must not take the caller down.
    if ALWAYS_CHILD or len(src) > IN_PROCESS_BYTES or not text.isascii():
        return _parse_in_child(src)
    return _parse_bytes(src)


def _parse_in_child(src):
    """_parse_bytes(src) in a child interpreter, killed at DEADLINE. It imports
    this very package (its root directory goes first), then the parent's
    sys.path, with -I so that neither its working directory nor PYTHON*
    variables can put other code first."""
    pkg = __package__
    root = Path(__file__).resolve().parents[len(pkg.split("."))]
    path = [str(root)] + [p for p in sys.path if p]
    # A memory cap in the child (Linux), so a runaway parse ends in MemoryError there
    # instead of the kernel's OOM killer picking a victim.
    cap = ("import resource; resource.setrlimit(resource.RLIMIT_AS, (%d, %d)); " % (CHILD_MEMORY, CHILD_MEMORY)
           if sys.platform == "linux" else "")
    code = (f"{cap}import sys, pickle; sys.path[:0] = {path!r}; from {pkg}.core import _parse_bytes; "
            "sys.stdout.buffer.write(pickle.dumps(_parse_bytes(sys.stdin.buffer.read())))")
    try:
        done = subprocess.run([sys.executable, "-I", "-c", code], input=src, capture_output=True,
                              timeout=DEADLINE, check=False)
    except subprocess.TimeoutExpired:
        return Result(TOO_BIG, reason=f"parsing took over {DEADLINE} s")
    if done.returncode < 0:                          # killed by a signal: out of memory, or a crash in the grammar
        return Result(TOO_BIG, reason=f"the parse child was killed (signal {-done.returncode})")
    if done.returncode == 1 and b"MemoryError" in done.stderr[-2000:]:
        return Result(TOO_BIG, reason=f"parsing needed over {CHILD_MEMORY >> 20} MB")
    if done.returncode:
        raise RuntimeError(f"bashtree's parse child failed ({done.returncode}): "
                           f"{done.stderr.decode('utf-8', 'replace')[-500:]}")
    return pickle.loads(done.stdout)                 # our own child's output, nothing else's


def _parse_bytes(src):
    tree = _parser().parse(src)
    for _ in range(MAX_REPAIRS):
        at = None if tree.root_node.has_error else _swallowed_newline(tree.root_node, src)
        if at is None:
            break
        src = src[:at] + b";" + src[at + 1:]
        tree = _parser().parse(src)
    if tree.root_node.has_error:
        return Result(SYNTAX_ERROR, reason="tree-sitter-bash reports a syntax error")
    walked = _walk(tree.root_node, src)
    if walked is None:
        return Result(TOO_BIG, reason=f"nested over {MAX_DEPTH} deep, or over {MAX_COMMANDS} commands")
    why = _unparsed(tree.root_node, src)
    if why:
        return Result(SYNTAX_ERROR, reason=why)
    commands = tuple(walked)
    # An unquoted reserved word where a command name goes: bash never runs a
    # command by that name. tree-sitter produces one when it misparses,
    # silently: `! { a; }` (21 of 162,750 real commands); and it accepts
    # `then x` where bash reports a syntax error.
    if any(c.words and c.words[0].raw in _RESERVED for c in commands):
        return Result(SYNTAX_ERROR, reason="a reserved word where a command name goes")
    return Result(OK, commands)

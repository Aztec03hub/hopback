"""Does a shell command start a new Claude Code conversation?

Only `claude` where a shell would actually run it counts: as the program of a
simple command, possibly behind wrappers (env, timeout, sudo, nohup, setsid,
nice, exec, command), or inside a command that tmux, `bash -c`, `script -c`,
screen, `( ... )` or `$( ... )` runs, or keys that `tmux send-keys` types. A
word that is merely an argument (`echo "claude is great"`, `pkill claude`,
`echo $(date) claude`), quoted punctuation (`echo ';' claude`), heredoc text,
a case pattern, a function body that is only defined, and arithmetic do not;
a `$( )` inside an unquoted heredoc, a `[[ ]]` or a case word does, as in bash.
Text without the literal word `claude` is never parsed, so a name spelled with
quotes or escapes (`cla"ude"`, `c\\laude`) is missed (the safe direction).

Parsing is tree-sitter-bash: a real grammar, chosen over the earlier
hand-written lexer and over bashlex by measurement on 162,750 real commands
(hopback-review/parser-bench/REPORT.md). This module only walks the tree and
decides.

Anything that isn't valid shell is not a launch: a miss leaves a session
visible, which is the safe direction. Known grammar limits (measured
2026-10-08 on real commands): two heredocs opened on one line (`cat <<a; cat
<<b`) are a syntax error to it, 175 real commands, none holding a launch; a
function whose body is a bare loop (`f() while ...; done`) hides a launch on
the line after it, and a heredoc terminator indented with spaces (which bash
rejects) is accepted, neither seen in real commands.
"""
import os
import re
import threading

# `claude <these>` doesn't begin a new conversation.
# From `claude --help` (2.1.289), plus older names.
SUBCOMMANDS = {"mcp", "plugin", "plugins", "config", "update", "upgrade", "doctor", "install",
               "agents", "attach", "auth", "auto-mode", "gateway", "import", "logs", "purge",
               "respawn", "rm", "stop", "kill", "ultrareview", "setup-token", "migrate-installer",
               "login", "logout", "rc", "remote-control"}
NOT_NEW_FLAGS = {"--version", "-v", "--help", "-h", "--resume", "-r", "--continue", "-c",
                 "--from-pr"}
# Wrappers, and how many value arguments each of their options takes.
WRAPPERS = {"env": {"-u": 1, "-C": 1, "-S": 1, "-P": 1, "--unset": 1, "--chdir": 1,
                    "--split-string": 1, "--argv0": 1},
            "timeout": {"-s": 1, "-k": 1, "--signal": 1, "--kill-after": 1},
            "sudo": {"-u": 1, "-g": 1, "-C": 1, "-D": 1, "-h": 1, "-p": 1, "-r": 1, "-t": 1,
                     "-U": 1, "-T": 1,
                     "--user": 1, "--group": 1, "--chdir": 1, "--host": 1, "--prompt": 1,
                     "--role": 1, "--type": 1, "--close-from": 1},
            "nice": {"-n": 1, "--adjustment": 1}, "nohup": {}, "setsid": {},
            "exec": {"-a": 1}, "command": {}, "stdbuf": {"-i": 1, "-o": 1, "-e": 1},
            "time": {"-f": 1, "-o": 1}, "ionice": {"-c": 1, "-n": 1, "--class": 1, "--classdata": 1}}
# A wrapper option (or short-option bundle) that makes it look up or list
# instead of running: `command -v claude`, `sudo -l claude`.
WRAPPER_QUERY = {"command": (set("vV"), set()),
                 "sudo": (set("lvkKe"), {"--list", "--validate", "--edit"})}


def _bundle_has(opt, letters, longs=()):
    """True if `opt` is one of `longs`, or a short-option bundle (`-lv`)
    containing any of `letters`. Plain string tests: a regex here backtracked
    quadratically on a long bundle."""
    if opt in longs:
        return True
    return (opt.startswith("-") and not opt.startswith("--") and opt[1:].isalpha()
            and bool(letters & set(opt[1:])))
TMUX_RUNS = {"new-window", "neww", "split-window", "splitw", "new-session", "new",
             "respawn-pane", "respawnp", "respawn-window", "respawnw"}
TMUX_TAKES_VALUE = set("tncFeslxyp") | {"-t", "-n", "-c", "-F", "-e", "-s", "-l", "-x", "-y", "-p"}
_ASSIGN = re.compile(r"^[A-Za-z_]\w*(\[[^]]*\])?\+?=")
# Bigger than any real command (largest seen: well under 1 MB). Parsing is
# linear, so this bounds the cost, not correctness.
MAX_COMMAND = 4 * 1024 * 1024
# tree-sitter-bash parses N heredocs in one command in O(N^2): 1,000 take
# 0.07 s, 40,000 take 56 s (measured 2026-10-08). The cap counts every `<<`
# (here-strings and quoted text too), so it errs towards giving up early; the
# most `<<` in one real command was 12.
MAX_HEREDOCS = 256
# Same for unclosed nested arrays (`a=(a=(a=(...`): 8,000 take 1.5 s, 50,000
# take 46 s (measured 2026-10-08). The most `=(` in one real command was 19.
MAX_ARRAYS = 1000
# Children of a `command` node that are words passed to it (redirects aren't).
_WORDS = {"word", "string", "raw_string", "ansi_c_string", "concatenation", "number",
          "simple_expansion", "expansion", "command_substitution", "translated_string",
          "process_substitution", "arithmetic_expansion"}
_LOCAL = threading.local()                          # a Parser isn't safe to share across threads


def _parser():
    if not hasattr(_LOCAL, "parser"):
        import tree_sitter_bash
        from tree_sitter import Language, Parser
        _LOCAL.parser = Parser(Language(tree_sitter_bash.language()))
    return _LOCAL.parser


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
_ANSI_C = re.compile(r"\\(x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}|[0-7]{1,3}|.)", re.S)
_ANSI_C_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
                  "e": "\x1b", "E": "\x1b", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c(text):
    out, pos = bytearray(), 0
    for m in _ANSI_C.finditer(text):
        out += text[pos:m.start()].encode("utf-8", "surrogatepass")
        pos, s = m.end(), m[1]
        if s[0] == "x" and len(s) > 1:
            out.append(int(s[1:], 16))
        elif s[0] in "01234567":
            out.append(int(s, 8) & 0xFF)
        elif s[0] in "uU" and len(s) > 1 and int(s[1:], 16) <= 0x10FFFF:
            out += chr(int(s[1:], 16)).encode("utf-8", "surrogatepass")
        else:
            out += _ANSI_C_SIMPLE.get(s, m[0]).encode("utf-8", "surrogatepass")
    out += text[pos:].encode("utf-8", "surrogatepass")
    return out.decode("utf-8", "replace")


def _text(node, src):
    """A word as the shell passes it on: quotes removed, escapes resolved.
    Expansions stay as written; nothing is evaluated."""
    raw = src[node.start_byte:node.end_byte].decode("utf-8", "replace")
    t = node.type
    if t == "raw_string":
        return raw[1:-1]
    if t == "ansi_c_string":
        return _ansi_c(raw[2:-1])
    if t == "word":
        return _unescape(raw, "")
    if t == "string":                                # in "...", \ escapes only these
        # The whole text, not its children: a newline between two lines of
        # the string is a gap between child nodes, and joining them lost it.
        return _unescape(raw[1:-1], '$`"\\\n')
    if t == "concatenation":
        return "".join(_text(c, src) for c in node.children)
    return raw


def _split(command):
    """Simple commands, as word lists, that would actually run.

    [] when the text isn't valid shell. bash's eval does run the complete
    commands before a syntax error, so a launch there is missed; measured on
    2026-10-08, none of 485 real commands with a syntax error held one, while
    walking broken trees added 11 false positives to the test table.
    Also [] past MAX_HEREDOCS `<<`, which no real command comes near."""
    if command.count("<<") > MAX_HEREDOCS or command.count("=(") > MAX_ARRAYS:
        return []
    src = command.encode("utf-8", "surrogatepass")
    tree = _parser().parse(src)
    if tree.root_node.has_error:
        return []
    out, stack = [], [tree.root_node]                # iterative: depth costs no stack frames
    while stack:
        n = stack.pop()
        if n.type == "function_definition":          # defined, not run
            continue
        if n.type == "command":
            words = []
            for c in n.children:
                if c.type == "variable_assignment":
                    words.append(src[c.start_byte:c.end_byte].decode("utf-8", "replace"))
                elif c.type == "command_name":
                    words.append(_text(c.children[0], src))
                elif c.type in _WORDS:
                    words.append(_text(c, src))
            if words:
                out.append(words)
        stack.extend(reversed(n.children))
    return out


def _skip_options(args, takes_value):
    """args after a command's own options. `takes_value` maps an option such
    as "-S" (or just "S") to how many values follow it; in a bundle of short
    options (`-dmS name`) the last letter decides."""
    counts = takes_value if isinstance(takes_value, dict) else {k: 1 for k in takes_value}
    letters = {k.lstrip("-")[-1:]: n for k, n in counts.items() if len(k.lstrip("-")) == 1}
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        flag = args[i]
        i += 1
        if flag == "--":
            break
        if flag in counts:
            i += counts[flag]
        elif not flag.startswith("--") and flag[-1:] in letters:
            i += letters[flag[-1:]]
    return args[i:]


def _runs_claude(tokens, depth, launch=None):
    """True if this simple command (a list of words) starts Claude. `launch`
    is the whole-command test used for text that is itself a command (bash
    -c, tmux, send-keys); it defaults to this module's is_launch."""
    launch = launch or is_launch
    if depth > 4:
        return False
    i = 0
    while i < len(tokens) and _ASSIGN.match(tokens[i]):
        i += 1                         # VAR=value prefixes
    tokens = tokens[i:]
    wrapped = 0
    while tokens and os.path.basename(tokens[0]) in WRAPPERS:
        wrapped += 1
        if wrapped > 8:
            return False               # no real command nests this deep
        name = os.path.basename(tokens[0])
        table = WRAPPERS[name]
        rest = _skip_options(tokens[1:], table)
        opts = tokens[1:len(tokens) - len(rest)]
        query = WRAPPER_QUERY.get(name)
        if query and any(_bundle_has(o, *query) for o in opts):
            return False
        if any(o.startswith("--") and "=" not in o and o != "--" and o not in table for o in opts):
            return False               # an option we can't size: don't guess the program
        j = 0
        while j < len(rest) and _ASSIGN.match(rest[j]):
            j += 1                     # env A=1 B=2 claude
        if name == "timeout" and j < len(rest):
            j += 1                     # the duration
        tokens = rest[j:]
    if not tokens:
        return False
    prog, args = os.path.basename(tokens[0]), tokens[1:]
    if prog == "claude":
        positional = [a for a in args if not a.startswith("-")]
        if set(positional[:2]) & SUBCOMMANDS:
            return False               # `claude mcp ...`, `claude --model m mcp ...`
        return not any(a in NOT_NEW_FLAGS or a.startswith(("--resume=", "--from-pr=")) for a in args)
    if prog in ("bash", "sh", "zsh", "dash", "script"):
        # `-c` (or a bundle such as `-lc`) among the options before the first
        # operand; `bash script.sh -c x` passes -c to the script instead.
        for k, a in enumerate(args):
            if not a.startswith("-") or a == "--":
                return False
            if not a.startswith("--") and "c" in a[1:]:
                return k + 1 < len(args) and launch(args[k + 1], depth + 1)
        return False
    if prog == "screen":
        opts = args[:len(args) - len(_skip_options(args, {"-S": 1, "-t": 1, "-c": 1}))]
        if any(_bundle_has(o, set("rRxXQ"), {"-ls", "-list", "-wipe"}) for o in opts):
            return False               # reattaching or querying, not starting
        return _runs_claude(args[len(opts):], depth + 1, launch)
    if prog == "tmux":
        rest = _skip_options(args, {"-L": 1, "-S": 1, "-f": 1})
        if not rest:
            return False
        sub, rest = rest[0], rest[1:]
        if sub in TMUX_RUNS:
            rest = _skip_options(rest, TMUX_TAKES_VALUE)
            if len(rest) == 1:
                return launch(rest[0], depth + 1)
            return _runs_claude(rest, depth + 1, launch)
        if sub in ("send-keys", "send"):
            # Typed keys run only when Enter follows them in the same call.
            rest = _skip_options(rest, {"-t", "-N"})
            enter = [j for j, key in enumerate(rest) if key in ("Enter", "C-m", "KPEnter")]
            if not enter:
                return False
            typed = " ".join(key for key in rest[:enter[-1]] if key not in ("Enter", "C-m", "KPEnter"))
            return launch(typed, depth + 1)
    return False


def is_launch(command, depth=0):
    """True if this shell command starts a new Claude Code conversation.

    Known limit: `tmux send-keys 'claude ...'` counts even when the pane it
    types into is already running Claude. Detection only acts on it if a new
    session's opening prompt also matches, so a stray message cannot by itself
    mark anything."""
    if not isinstance(command, str) or "claude" not in command or len(command) > MAX_COMMAND:
        return False
    return any(_runs_claude(cmd, depth) for cmd in _split(command))


def self_check():
    """Launches and look-alikes this must tell apart; run by the test suite."""
    yes = [
        "claude 'do it'", "cd /w && claude --model sonnet 'x'", "cd /w\nclaude 'x'",
        "env CLAUDE_EFFORT=low claude --effort low 'x'", "env -i claude", "timeout 300 claude -p x",
        "timeout -s KILL 300 claude", "sudo -u u claude", "setsid claude", "nohup claude &",
        "~/.local/bin/claude 'x'", "/home/u/.local/bin/claude", "$HOME/.local/bin/claude x", "\\claude x",
        'tmux new-window -d -n e13 -c /w "claude --dangerously-skip-permissions \\"$(cat $X/p.txt)\\""',
        "tmux new-window -d -n lane1 claude 'x'", "tmux split-window -h claude --model m",
        "tmux split-window -p 30 claude x",
        "tmux send-keys -t w 'claude --dangerously-skip-permissions' C-m",
        "tmux send-keys -t w claude Enter",
        "P=$(cat /tmp/p.txt); tmux send-keys -t $P 'claude --dangerously-skip-permissions' Enter",
        "screen -dmS a claude x", "bash -c 'cd /w && claude x'", "bash -lc 'claude x'",
        "(claude 'x')", "(cd x && claude 'y')", "echo start; claude 'x'", "echo $(claude -p x)",
        "echo `claude -p x`", "claude $(cat p.txt)", "x=$(claude -p 'q')",
        "if true; then claude x; fi", "for i in 1 2; do claude x; done", "while true; do claude x; done",
        "! claude x", "case $1 in\n  go) claude x ;;\nesac",
        "ID=$(tmux split-window -v -d -P -F '#{pane_id}' -t %106 \"cd /w && claude -n x \\\"$P\\\"\")",
        "set -e\nP='You are \"x\"'\ntmux new-window -d \"claude '$P'\"",
        "python3 - <<'EOF'\nprint(1)\nEOF\ntmux new-window -d -n e16 -c /w \\\n  \"claude --dangerously-skip-permissions x\"",
        "cat > p.txt <<'EOF'\nprompt\nEOF\nclaude \"$(cat p.txt)\"",
        "cat <<-EOF\n\tbody\n\tEOF\nclaude x", "diff <(claude -p a) b", "script -qc 'claude x' /dev/null",
        "tmux new-window -t s:1 -n w -c /d claude x", "f() { echo; }\nclaude x",
        "f() { { echo; }; }\nclaude x", "case $x in a) echo;; esac\nclaude x",
        "f() ( echo )\nclaude x", "X=1 claude y",
        "ionice -c 3 claude x", "stdbuf -oL claude x", "nice -n 5 claude x", "time claude x",
        "exec claude x", "sudo --user u claude x", "env --chdir /w claude x", "screen -L claude",
        "cat <<<\"text\"\nclaude x", "f() if true; then echo if; fi\nclaude x",
        "echo \"$(date)\"; claude x", "x=\"$(echo \"a b\")\" && claude y", "true &>/dev/null && claude x",
        "exec >/tmp/log; claude x", ">out.txt claude x", "x=$(printf x) claude",
        "cat <<EOF\n$(claude -p x)\nEOF", "[[ -n $(claude -p x) ]]",
        "case $(claude -p x) in a) echo;; esac",
        "tmux send-keys -t w \"cd /w && claude \\\"\\$(cat p)\\\"\" Enter",
        "claude " + "a" * 300000,
        "bash -c \"cd /w\nclaude x\"", "tmux new-window -d \"cd /w\nclaude x\"",
        "bash -c \"echo hi # note\nclaude x\"",
        "bash -c $'cd /w\\nclaude x'",
    ]
    no = [
        "pkill claude", "rg claude docs", "echo \"claude is great\"", "git commit -m 'docs: claude handoff'",
        "cat > H.md <<'EOF'\nhandoff text\nRun `claude` to start.\nclaude --resume x\nEOF\nls",
        "claude mcp add x", "claude --version", "claude --resume abc", "claude --model m --resume abc",
        "claude -c", "which claude", "ls ~/.claude/projects", "cat claude.md", "rg --glob=*.md claude docs",
        "echo 'unbalanced", "npm view @anthropic-ai/claude-code version", "ps aux | grep claude",
        "tmux capture-pane -p -t claude", "claude plugin install x", "x=claude; echo $x",
        "command -v claude", "x=$(command -v claude)", "bash -c 'command -v claude'", "command -V claude",
        "command -pv claude", "sudo -l claude", "sudo --list claude", "sudo -ll claude",
        "claude() { command claude \"$@\"; }", "claude () {\n  claude x\n}", "function f {\n claude x\n}",
        "f() {\n  claude x\n}", "case \"$1\" in\n  claude) echo hi;;\nesac",
        "case $x in\n  claude|c) echo ;;\nesac", "case $x in (claude) echo;; esac",
        "git commit -m \"title\n\nclaude\"", "echo \"a\nclaude\"", "rg -n \"foo\nclaude\" docs",
        "screen -r claude", "screen -x claude", "screen -d -r claude", "screen -dr claude", "screen -DR claude",
        "cat > f <<'EOF'\nclaude x\n", "cat <<\\EOF\nclaude x\nEOF", "bash script.sh -c claude",
        "cat <<A <<B\nx\nA\nclaude y\nB", "cat <<\"END OF\"\nclaude x\nEND OF",
        "claude auth login", "claude login", "claude --model m mcp list", "claude --add-dir x doctor",
        "(" * 300000 + "claude",
        "echo $(date) claude", "ls $(pwd) claude", "rg -n x $(git ls-files) claude", "echo $(date)claude",
        "echo $((1+2)) claude", "[[ $x == @(claude|codex) ]]", "echo ${x%%(claude)}",
        "(( claude ))", "echo ';' claude", "echo \"|\" claude", "echo '&&' claude", "find . -exec ls {} \\; claude",
        "tmux send-keys -t w ';' claude", "tmux send-keys -t w 'claude x'", "# claude x", "echo hi # ; claude x", "for x in claude codex; do echo $x; done",
        "printf '%s\\n' claude", "export X=claude", "alias c=claude", "type claude", "hash claude",
        "test -x claude", "[[ -x claude ]]", "ssh host claude",
        "f() { { echo; }; claude x; }", "run() {\n  cd \"$1\" || { echo bad; return 1; }\n  claude \"$2\"\n}",
        "f() { [ -n \"$x\" ] && { echo hi; }; claude x; }", "[[ $x =~ (claude|codex) ]]",
        "if [[ $c =~ (a|claude) ]]; then echo; fi", "[[ $x =~ (claude) ]] && echo hi",
        "f() ( claude x )", "f () ( claude x )", "f() if true; then claude x; fi",
        "f() while true; do claude x; done", "claude ultrareview", "claude rc", "claude remote-control",
        "claude --resume=abc", "cat <<-EOF\nclaude x\n\tEOF", "echo ${x:-$(echo) claude}",
        "screen -X stuff claude", "echo \"unterminated claude", "claude \"unterminated",
        "echo ${x:+;} claude",
        "tmux new-window -n claude", "tmux new-window -t claude echo",
        "f() while true; do for i in 1; do echo; done; claude x; done",
        "f() for i in a; do for j in b; do echo; done; claude x; done",
        "f() while true; do echo done; claude x; done", "f() if true; then echo fi; claude x; fi",
        "sudo --user claude ls", "sudo --user claude bash", "sudo --group claude id",
        "env --unset claude ls", "env --chdir claude ls", "env --weird-option claude",
        "tmux new-window -S -n claude top", "[[ $x == \"]]\" claude ]]", "[[ $x == \\]\\] claude ]]",
        "case $x in $(echo a)|claude) echo;; esac", "case x in a) echo;; claude) echo;; esac",
        "case x in a) echo;; (claude) echo;; esac", "cat <<%\nclaude x\n%", "cat <<@@\nclaude x\n@@",
        "echo `date; claude x", "echo ` claude x", "echo `date` claude", "claude -r", "claude -v",
        "\"done\"; claude-ish x",
        "function launch () {\n  tmux new-window -d \"claude $1\"\n}", "function f () { claude x; }",
        "function f ( ) { claude x; }", "function f ()\n{\n claude x\n}",
        "echo \"n=$(rg \"a|claude b\" f)\"", "echo \"ok: $(date \"+%H; claude x\")\"",
        "git commit -m \"$(git log -1 --format=\"%s && claude fix\")\"",
        "echo hi >& claude", "echo hi 2>&1 claude", "env -P claude ls", "sudo -U claude -l",
        "f() while true; do { if x; then y; fi; claude z; }; done",
        "env -u claude ls", "env -i -u claude ls", "ls !(claude)", "rm -- +(claude)",
        "exec 2>>$HOME/.cache/claude", "exec >/tmp/claude", "exec 3>/var/log/claude echo hi",
        "nohup 2>/tmp/claude ls", "2>/tmp/claude ls", "claude --from-pr 12", "claude --from-pr=12", "cat <<'EOF'\n$(claude -p x)\nEOF", "bash -c \"echo hi # note claude x\"", "bash -c $'echo claude'", "claude $'\\u0072m'",
    ] + [f"claude {sub}" for sub in sorted(SUBCOMMANDS)] + [
        "bash -c " * 6 + "claude", "env " * 16000 + "claude",
    ]
    bad = [c for c in yes if not is_launch(c)] + [c for c in no if is_launch(c)]
    assert not bad, bad
    import time
    for slow in ("sudo -" + "v" * 60000 + "! claude", "command -" + "v" * 60000 + "! claude",
                 "screen -" + "r" * 60000 + "! claude", "env " * 16000 + "claude",
                 "`" * 60000 + "claude", "(" * 60000 + "claude", '"$(' * 3000 + "claude",
                 'echo "$(echo ' * 1200 + 'claude"', "x <<a " * 10000 + "\n" + "a\n" * 10000 + "claude", "a=(" * 20000 + "claude"):
        t = time.monotonic()
        is_launch(slow)
        # A fixed bound with room for a busy machine: the failures this
        # guards against took tens of seconds, not tenths.
        assert time.monotonic() - t < 2.0, ("slow input", slow[:40])
    print(f"ok: {len(yes)} launches, {len(no)} non-launches")


if __name__ == "__main__":
    self_check()

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

Parsing, wrappers and nested shells come from bashtree (vendored in
hopback/_vendor/bashtree, shared with bash-write-guard; its docstring lists the
grammar's known limits). This module only decides what counts as a launch.
Anything bashtree can't parse is not a launch: a miss leaves a session
visible, which is the safe direction.
"""
import os

from ._vendor.bashtree import nested, parse, unwrap

# `claude <these>` doesn't begin a new conversation.
# From `claude --help` (2.1.289), plus older names.
SUBCOMMANDS = {"mcp", "plugin", "plugins", "config", "update", "upgrade", "doctor", "install",
               "agents", "attach", "auth", "auto-mode", "gateway", "import", "logs", "purge",
               "respawn", "rm", "stop", "kill", "ultrareview", "setup-token", "migrate-installer",
               "login", "logout", "rc", "remote-control"}
NOT_NEW_FLAGS = {"--version", "-v", "--help", "-h", "--resume", "-r", "--continue", "-c",
                 "--from-pr"}
# Bigger than any real command (largest seen: well under 1 MB).
MAX_COMMAND = 4 * 1024 * 1024
# Nested shells hopback follows. eval is left out: it was never counted, and
# a miss is the safe direction.
_FOLLOWS = {"bash", "sh", "zsh", "dash", "script", "screen", "tmux", "tmux send-keys"}


def _split(command):
    """Word lists of the simple commands that would run (a function body that
    is only defined doesn't). [] if unparseable: bashtree then has none."""
    return [[w.text for w in c.words] for c in parse(command).commands
            if c.kind == "simple" and not c.inside("function_body")]


def _runs_claude(tokens, depth, launch=None):
    """True if this simple command (a list of words) starts Claude. `launch`
    is the whole-command test used for text that is itself a command (bash
    -c, tmux, send-keys); it defaults to this module's is_launch."""
    launch = launch or is_launch
    if depth > 4:
        return False
    u = unwrap(tokens)
    # A variable-built path (`$HOME/.local/bin/claude`) is still the program; the
    # name is literal, only the directory isn't.
    if (u.stopped and u.stopped != "nonliteral-program") or not u.words:
        return False                   # a lookup, or a wrapper we can't see past
    prog, args = os.path.basename(u.words[0]), u.words[1:]
    if prog == "claude":
        positional = [a for a in args if not a.startswith("-")]
        if set(positional[:2]) & SUBCOMMANDS:
            return False               # `claude mcp ...`, `claude --model m mcp ...`
        return not any(a in NOT_NEW_FLAGS or a.startswith(("--resume=", "--from-pr=")) for a in args)
    for n in nested(u.words):
        if n.via not in _FOLLOWS:
            continue
        if n.kind == "text" and launch(n.text, depth + 1):
            return True
        if n.kind == "words" and _runs_claude(list(n.words), depth + 1, launch):
            return True
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

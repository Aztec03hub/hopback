"""Mutation check for spawn detection: remove each guard from a temporary copy of
the code, and the spawn test must then fail. A guard whose removal still passes
is not protected by any test.

    python tests/mutate_launches.py      (a few seconds, mutants run in parallel)
"""
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
# name -> [(file, old text, new text)]
L, S, C = "launches.py", "shellparse.py", "cli.py"
MUTANTS = {
    "no quote stripping": [(L, '_STRIP.sub("", text)', "text")],
    "no send-keys scan": [(L, 'if (isinstance(c, str) and "tmux" in c and any(k in c for k in _TYPES_TEXT)\n'
                              '                    and frag in norm(c)):', "if False:")],
    "wide time window": [(L, "WINDOW = 30.0", "WINDOW = 400.0")],
    "any claude mention launches": [(L, "from .shellparse import is_launch",
                                     "is_launch = lambda c: 'claude' in c")],
    "prompt file need not be named": [(L, "    if not files:\n        return None\n", ""),
                                      (L, "        if files:\n            start",
                                          "        if True:\n            start"),
                                      (L, "os.path.basename(path) in files and ", ""),
                                      (L, "if any(f in c for f in files) and frag in norm(c):",
                                          "if frag in norm(c):")],
    "a Read path counts as sent": [(L, '            if name == "Write":',
                                       '            if any(frag in norm(str(v)) for v in inp.values()):\n'
                                       '                return True\n            if name == "Write":')],
    "copies count as two launches": [(L, "by_launch.setdefault(t, []).append(p)",
                                         "by_launch.setdefault((t, p), []).append(p)")],
    "unreadable parent hidden": [(L, "            unreadable += 1\n            continue", "            continue")],
    "settles no while unreadable": [(L, "if unreadable or now - ", "if now - ")],
    "hidden spawns spend -n": [("adapters/claude.py", 'hidden = role == "spawn" and not include_teams', "hidden = False")],
    "marks need the search box": [(C, 'if (self.view_review and event.character in ("0", "1", "2", "3")\n                    and not isinstance(self.focused, Input)):', "if False:")],
    "acts on a stale list": [(C, "            if self._shown_ticket != self._refresh_ticket:", "            if False:")],
    # the tree walk
    "function bodies run": [(S, 'if n.type == "function_definition":', 'if False:')],
    "syntax errors parsed": [(S, "    if tree.root_node.has_error:\n        return []", "")],
    "no heredoc cap": [(S, 'command.count("<<") > MAX_HEREDOCS or ', "")],
    "no array cap": [(S, ' or command.count("=(") > MAX_ARRAYS', "")],
    "--from-pr= is new": [(S, '("--resume=", "--from-pr=")', '"--resume="')],
    "escapes in \"...\" kept": [(S, "        return _unescape(raw[1:-1], '$`\"\\\\\\n')", "        return raw[1:-1]")],
    "string lines glued": [(S, "        return _unescape(raw[1:-1], '$`\"\\\\\\n')",
                            "        return \"\".join(_text(c, src) for c in node.children if c.type != '\"')")],
    "escapes in words kept": [(S, '        return _unescape(raw, "")', "        return raw")],
    "'...' quotes kept": [(S, '    if t == "raw_string":\n        return raw[1:-1]', '    if t == "raw_string":\n        return raw')],
    "assignment is the program": [(S, "    while i < len(tokens) and _ASSIGN.match(tokens[i]):\n        i += 1                         # VAR=value prefixes",
                                      "    while False:\n        i += 1")],
    # the decision
    "wrapper queries run": [(S, "        if query and any(_bundle_has(o, *query) for o in opts):\n            return False",
                                "        if False:\n            return False")],
    "env -u value runs": [(S, '"env": {"-u": 1, ', '"env": {')],
    "--from-pr is new": [(S, '                 "--from-pr"}', '                 }')],
    "bash -c anywhere": [(S, '            if not a.startswith("-") or a == "--":\n                return False',
                             '            if not a.startswith("-") or a == "--":\n                continue')],
    "screen reattach runs": [(S, "            return False               # reattaching or querying, not starting",
                                 "            pass")],
    "subcommand after flags": [(S, "        if set(positional[:2]) & SUBCOMMANDS:", "        if set(args[:1]) & SUBCOMMANDS:")],
    "send-keys without Enter": [(S, "            if not enter:\n                return False", "            if not enter:\n                enter = [len(rest)]")],
    "script -c ignored": [(S, '"zsh", "dash", "script")', '"zsh", "dash")')],
    "tmux values not skipped": [(S, "            rest = _skip_options(rest, TMUX_TAKES_VALUE)", "            rest = _skip_options(rest, {})")],
    "--resume= is new": [(S, '("--resume=", "--from-pr=")', '"--from-pr="')],
    "unknown long wrapper option": [(S, "            return False               # an option we can't size", "            pass  #")],
    "tmux -S takes a value": [(S, 'set("tncFeslxyp")', 'set("tncFeslxySp")')],
}
TEST = ("import sys; sys.path.insert(0, 'tests'); import test_hopback as t; "
        "t.test_shell_launch_parser(); t.test_spawned_sessions(); t.test_review_marking()")


def run(item):
    name, edits = item
    with tempfile.TemporaryDirectory(prefix="hopback-mut-") as tmp:
        d = Path(tmp)
        shutil.copytree(SRC / "hopback", d / "hopback")
        shutil.copytree(SRC / "tests", d / "tests")
        for fname, a, b in edits:
            f = d / "hopback" / fname
            text = f.read_text()
            if a not in text:
                return f"{name:32} -> STALE (mutation no longer applies)"
            f.write_text(text.replace(a, b))
        try:
            res = subprocess.run([sys.executable, "-c", TEST], cwd=d, capture_output=True,
                                 text=True, timeout=60)
            if res.returncode and "AssertionError" not in res.stderr:
                return f"{name:32} -> ERROR, not an assertion: {res.stderr.strip().splitlines()[-1][:80]}"
            return f"{name:32} -> {'killed' if res.returncode else 'SURVIVED'}"
        except subprocess.TimeoutExpired:
            return f"{name:32} -> killed (timeout)"


if __name__ == "__main__":
    # Control: the unmodified copy must pass, or "killed" means nothing.
    control = run(("control (no mutation)", []))
    print(control.replace("SURVIVED", "passes, as it must"))
    if "SURVIVED" not in control:
        sys.exit("the test fails without any mutation; fix that first")
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(run, MUTANTS.items()))
    print("\n".join(results))
    sys.exit(0 if all(" killed" in r for r in results) else 1)

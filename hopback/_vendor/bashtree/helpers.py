"""Generic helpers on a command's words: see through wrappers (`sudo git`,
`timeout 5 make`), and find the commands a command runs in turn (`bash -c
'...'`, `tmux new-window '...'`, `xargs rm`). No policy: what the inner
command means is the caller's call.

    u = unwrap(["sudo", "-u", "x", "git", "push"])
    u.words == ("git", "push"), u.wrappers == ("sudo",), u.stopped is None
    for n in nested(u.words): n.via, n.kind ("text" | "words" | "unknown"), n.text / n.words

Options are read the way getopt reads them, from a table per program. An
option the table doesn't list could take a value or not, so it is never
guessed at: unwrap() reports `stopped="unsized-option"` and nested() reports
kind "unknown".
"""
import itertools
import os
import posixpath
import re
import shlex
from dataclasses import dataclass


class Opts:
    """A program's options. `flags`: short letters with no value. `values`:
    short letters with one, attached (`-uroot`) or the next argument.
    `optional`: short letters whose value can only be attached (`-e[eof]`).
    `longs`: space-separated names; `name=` takes a value (`--x=v` or `--x v`),
    `name=?` only an attached one."""

    def __init__(self, flags="", values="", optional="", longs=""):
        self.flags, self.values, self.optional = flags, values, optional
        self.longs = {}
        for n in longs.split():
            self.longs[n.rstrip("=?")] = "opt" if n.endswith("=?") else 1 if n.endswith("=") else 0


def getopt(args, spec, permute=False, until=()):
    """(options, operands, ok). `options` is [(name, value or None)]. Without
    `permute` the operands start at the first non-option, as for `sudo` and
    `env`; with it, options may follow operands, as for `su`. Reading stops
    right after an option named in `until`: `operands` is then everything
    after it, unread. ok is False on an option `spec` doesn't list, an
    ambiguous `--prefix`, or a missing value: then `operands` holds the
    unread words."""
    opts, operands, i = [], [], 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            return opts, operands + args[i:], True
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            hits = [name] if name in spec.longs else [n for n in spec.longs if n.startswith(name)]
            if len(hits) != 1:                           # getopt_long takes a unique prefix
                return opts, operands + args[i - 1:], False
            arity = spec.longs[hits[0]]
            if eq and arity == 0:
                return opts, operands + args[i - 1:], False
            if arity == 1 and not eq:
                if i == len(args):
                    return opts, operands + args[i - 1:], False
                val, eq = args[i], "="
                i += 1
            opts.append(("--" + hits[0], val if eq else None))
            if "--" + hits[0] in until:
                return opts, operands + args[i:], True
        elif a.startswith("-") and a != "-":
            body = a[1:]
            for k, ch in enumerate(body):
                if ch in spec.values or ch in spec.optional:
                    val = body[k + 1:]                   # the rest of the bundle is its value
                    if not val and ch in spec.values:
                        if i == len(args):
                            return opts, operands + args[i - 1:], False
                        val = args[i]
                        i += 1
                    opts.append(("-" + ch, val if ch in spec.values else val or None))
                    if "-" + ch in until:
                        return opts, operands + args[i:], True
                    break
                if ch not in spec.flags:
                    return opts, operands + args[i - 1:], False
                opts.append(("-" + ch, None))
        elif permute:
            operands.append(a)
        else:
            return opts, operands + args[i - 1:], True
    return opts, operands, True


_COMMON = "help version"
# Wrappers: they run their operands as a command. GNU coreutils 9, sudo 1.9,
# util-linux; `!`, `coproc`, `builtin` and bash's `exec` and `command`.
WRAPPERS = {
    # -a (--argv0) is coreutils 9.5+; -P is BSD env's; timeout's -f and -p are
    # newer coreutils' short forms. Listing them only ever peels too little.
    "env": Opts("i0v", "uCSPa", longs="ignore-environment null unset= chdir= split-string= argv0= "
                "block-signal=? default-signal=? ignore-signal=? list-signal-handling debug " + _COMMON),
    "timeout": Opts("fpv", "sk", longs="signal= kill-after= foreground preserve-status verbose " + _COMMON),
    "sudo": Opts("AbBEeHiKklNnPSsVv", "aCcDgpRrtTUu", "h",
                 "askpass background bell close-from= chdir= preserve-env=? edit group= set-home "
                 "help host= login remove-timestamp reset-timestamp list non-interactive "
                 "preserve-groups prompt= chroot= role= stdin shell type= command-timeout= no-update "
                 "login-class= auth-type= "
                 "other-user= user= version validate"),
    "nice": Opts("0123456789", "n", longs="adjustment= " + _COMMON),   # `nice -10 cmd`
    "nohup": Opts(longs=_COMMON), "setsid": Opts("cfw", longs="ctty fork wait " + _COMMON),
    "exec": Opts("cl", "a"), "command": Opts("pvV"), "builtin": Opts(), "coproc": Opts(), "!": Opts(),
    "stdbuf": Opts(values="ioe", longs="input= output= error= " + _COMMON),
    "time": Opts("pavqV", "fo", longs="portability append verbose quiet format= output= " + _COMMON),
    "ionice": Opts("t", "cnpPu", longs="class= classdata= pid= pgid= uid= ignore " + _COMMON),
    "busybox": Opts(),
}
# Options that make a wrapper look something up instead of running its
# operands (`command -v git`, `sudo -l`, `ionice -p 12`): stopped="query".
WRAPPER_QUERY = {"command": {"-v", "-V"},
                 "sudo": {"-l", "-v", "-K", "-V", "--list", "--validate", "--remove-timestamp",
                          "--version", "--help"},
                 "ionice": {"-p", "-P", "-u", "--pid", "--pgid", "--uid"}}
# Options that make it edit the files named instead: stopped="edit".
WRAPPER_EDIT = {"sudo": {"-e", "--edit"}}
# A path-qualified wrapper (`/usr/bin/env`) is only seen through from these.
SYSTEM_DIRS = {"/bin", "/usr/bin", "/sbin", "/usr/sbin"}   # not /usr/local/bin: often user-writable
MAX_WRAPPERS = 8                                    # no real command nests deeper
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")


@dataclass(frozen=True)
class Unwrapped:
    words: tuple       # the program that really runs, and its arguments
    wrappers: tuple    # the wrapper names peeled off, outermost first
    stopped: str | None   # why `words` isn't the program that runs, or None:
    #                       "query" (`command -v x`), "edit" (`sudo -e f`),
    #                       "unsized-option" (an option we can't size),
    #                       "untrusted-path" (`words` starts with `./sudo` or the like),
    #                       "too-deep" (over MAX_WRAPPERS),
    #                       "nonliteral-assignment" (a Word like "${a}_X=1": maybe an assignment),
    #                       "nonliteral-program" (the program word is `$S`, a glob, ...: `words` from it on),
    #                       "nonliteral-operand" (a wrapper option value or the timeout duration is `$T`:
    #                       `words` from that wrapper on)
    options: tuple = ()   # per wrapper, its options as (name, value) pairs:
    #                       `time -o f` writes f


def _trusted(word):
    """A bare name, or a path into a system directory (`/usr//bin/x` and
    `/usr/bin/./x` too; `..` and symlinks are not followed, so any `..` fails)."""
    if "/" not in word:
        return True
    d = posixpath.normpath(posixpath.dirname(word))
    d = d[1:] if d.startswith("//") and not d.startswith("///") else d
    return ".." not in word.split("/") and d in SYSTEM_DIRS


_NONLIT = re.compile(r"[$`*?{]")      # a plain string cannot say it was quoted; `[` is the test builtin


def _nonlit(text, literal=True):
    return not literal or bool(_NONLIT.search(text))


def _wrapper(word):
    name = os.path.basename(word)
    return name if name in WRAPPERS and _trusted(word) else None


def unwrap(words):
    """Peel VAR=value prefixes and wrappers off a command's words. Words may be
    strings or Word objects (`.text`, `.literal`); a non-literal word that looks
    like an assignment (`"${a}_X=1"`) stops with "nonliteral-assignment"."""
    lits = [getattr(w, "literal", True) for w in words]
    words = [getattr(w, "text", w) for w in words]
    peeled, options = [], []
    again = False                                        # env -S: the pieces re-enter the loop as the same env

    def bad(k):                                          # words[k] might be an assignment we can't read
        return not lits[len(lits) - len(words) + k] and "=" in words[k] and not _ASSIGN.match(words[k])

    def nonlit(k):
        return Unwrapped(tuple(words[k:]), tuple(peeled), "nonliteral-assignment", tuple(options))
    i = 0
    while i < len(words) and (_ASSIGN.match(words[i]) or bad(i)):
        if bad(i):
            return nonlit(i)
        i += 1
    words = words[i:]
    lits = lits[i:]
    while words and (name := _wrapper(words[0])):
        if len(peeled) >= MAX_WRAPPERS:
            return Unwrapped(tuple(words), tuple(peeled), "too-deep", tuple(options))
        opts, rest, ok = getopt(words[1:], WRAPPERS[name], until={"-S", "--split-string"} if name == "env" else ())
        if again:
            options[-1] += tuple(opts)
            again = False
        else:
            peeled.append(name)
            options.append(tuple(opts))
        names = {o for o, _ in opts}

        def bad2(r, k):
            return not lits[len(lits) - len(r) + k] and "=" in r[k] and not _ASSIGN.match(r[k])

        def stop(why, rest=rest):
            return Unwrapped(tuple(rest), tuple(peeled), why, tuple(options))
        if not ok:
            return stop("unsized-option")
        if (names & WRAPPER_QUERY.get(name, set()) or names & {"--help", "--version"}
                or (name == "sudo" and ("-h", None) in opts)):   # -h alone is help, -hHOST a host
            return stop("query")
        if names & WRAPPER_EDIT.get(name, set()):
            return stop("edit")
        used = [lits[k] and not _NONLIT.search(words[k]) for k in range(1, len(words) - len(rest))]
        if name == "timeout" and rest:
            used.append(lits[len(lits) - len(rest)] and not _NONLIT.search(rest[0]))
        if name == "env" and names & {"-S", "--split-string"}:   # only the -S value itself may be non-literal
            used = [u for k, u in enumerate(used, 1) if not (words[k - 1] in ("-S", "--split-string") or words[k].startswith("--split-string="))]
        if not all(used):                             # word splitting could shift which program runs
            return Unwrapped(tuple(words), tuple(peeled), "nonliteral-operand", tuple(options))
        j = 0
        if name == "env":
            split = [v for o, v in opts if o in ("-S", "--split-string")]
            if split:
                # `env -S 'cmd args'`: its own quoting, escapes and ${VAR}; only
                # plain words and quotes are read here. The pieces take the
                # option's place, and env reads on from them: `rest` is unread.
                if any(ch in split[0] for ch in "$\\#"):
                    return stop("unsized-option")
                lex = shlex.shlex(split[0], posix=True)
                lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r\n\v\f", True, ""
                try:
                    pieces = list(lex)
                except ValueError:
                    return stop("unsized-option")
                words = [words[0]] + pieces + rest
                lits = [True] * (1 + len(pieces)) + lits[len(lits) - len(rest):]
                again = True
                continue
            if rest[:1] == ["-"]:
                j = 1                                    # a lone `-` is -i
            while j < len(rest) and "=" in rest[j]:
                if bad2(rest, j):
                    return Unwrapped(tuple(rest[j:]), tuple(peeled), "nonliteral-assignment", tuple(options))
                j += 1                                   # env A=1 B=2 cmd: any word with =
        elif name in ("time", "!", "coproc"):            # keywords: `time X=1 cmd` runs cmd with X set
            while j < len(rest) and (_ASSIGN.match(rest[j]) or bad2(rest, j)):
                if bad2(rest, j):
                    return Unwrapped(tuple(rest[j:]), tuple(peeled), "nonliteral-assignment", tuple(options))
                j += 1
        elif name == "sudo":
            while j < len(rest) and (_ASSIGN.match(rest[j]) or bad2(rest, j)):
                if bad2(rest, j):
                    return Unwrapped(tuple(rest[j:]), tuple(peeled), "nonliteral-assignment", tuple(options))
                j += 1
            if names & {"-s", "-i", "--shell", "--login"} and not rest[j:]:
                return stop("unsized-option")            # a shell that reads its commands from stdin
            if names & {"-s", "-i", "--shell", "--login"} and any("$" in a for a in rest[j:]):
                return stop("unsized-option")    # the shell it starts expands `$` in the operands
        elif name == "timeout" and rest:
            j = 1                                        # the duration
        words = rest[j:]
        lits = lits[len(lits) - len(words):] if words else []
    # `./sudo x`, `/usr/local/bin/env x`: a wrapper's name from a path we don't trust
    stopped = "untrusted-path" if words and os.path.basename(words[0]) in WRAPPERS else None
    if words and _nonlit(words[0], lits[0]):
        stopped = "nonliteral-program"
    return Unwrapped(tuple(words), tuple(peeled), stopped, tuple(options))


@dataclass(frozen=True)
class Nested:
    via: str           # the command that runs it: bash, tmux, screen, eval, xargs, find ...
    kind: str          # "text": shell text, parse it; "words": an argv; "unknown": runs
    #                    something that can't be known statically (a script, stdin, xargs)
    text: str = ""
    words: tuple = ()


# Shells whose -c is a flag: the text is the first operand after the options.
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ksh93", "mksh", "ash", "rbash", "yash", "posh"}
SHELL_VALUE_OPTS = set("oO")                        # `-o pipefail`, `+O extglob`
SHELL_VALUE_LONGS = {"--rcfile", "--init-file", "--emulate"}
# Programs whose -c takes the text as its value.
C_TAKES_TEXT = {
    "fish": Opts("ilnNPvh", "cCdopf", longs="command= init-command= debug= debug-output= profile= "
                 "features= interactive login no-execute no-config private print-rusage-self " + _COMMON),
    "csh": Opts("bdefFimnqstvVxX", "c"), "tcsh": Opts("bdefFimnqstvVxXl", "c"),
    "script": Opts("aefqhV", "cEBIOTmo", "t", "append command= echo= log-in= log-out= log-io= "
                   "log-timing= logging-format= flush force quiet output-limit= return timing=? " + _COMMON),
}
SU = Opts("flmpPhV", "cgGswu", longs="command= session-command= fast group= supp-group= login "
          "preserve-environment pty shell= whitelist-environment= user= " + _COMMON)   # -u: runuser
SCREEN = Opts("aAdDfiIlLmOqrRUvxX", "cehpsStT")
TMUX = Opts("2CDlNuvV", "cfLST")
# tmux commands that run a shell command: name -> (alias, options, role).
# "shell": the operands are a shell command; "if": the first operand is one,
# the rest are tmux commands; "keys": typed keys.
TMUX_CMDS = {
    "new-window": ("neww", Opts("abdkPS", "ceFnt"), "shell"),
    "split-window": ("splitw", Opts("bdfhIvPZ", "celptF"), "shell"),   # -p: deprecated, accepted
    "new-session": ("new", Opts("AdDEPX", "cefFnstxy"), "shell"),
    "respawn-pane": ("respawnp", Opts("k", "cet"), "shell"),
    "respawn-window": ("respawnw", Opts("k", "cet"), "shell"),
    "run-shell": ("run", Opts("bC", "cdt"), "shell"),
    "pipe-pane": ("pipep", Opts("IOo", "t"), "shell"),
    "display-popup": ("popup", Opts("BCEkN", "bcdehsStTwxy"), "shell"),
    "if-shell": ("if", Opts("bF", "t"), "if"),
    "send-keys": ("send", Opts("FHKlMRX", "cNt"), "keys"),
    "command-prompt": (None, Opts(), "unknown"),
    "confirm-before": ("confirm", Opts(), "unknown"),
}
# tmux commands, and their aliases, that never run a shell command (not lock-*:
# lock-command; not choose-*: a template command).
TMUX_QUIET = re.compile(r"(?:list|show|kill|select|has|rename|resize|capture|swap|move|last|next|"
                        r"previous|refresh|clear|wait|switch|attach|display-message|copy-mode|"
                        r"clock|server-info|start-server|suspend|break|join|link|"
                        r"unlink|rotate|find-window|delete-buffer|set-buffer)[\w-]*\Z|"
                        r"(?:ls|lsw|lsp|lsc|lsk|lsb|lscm|a|at|has|display|killp|killw|selectp|"
                        r"selectw|capturep|switchc|refresh|renamew|rename|resizep|resizew|swapp|"
                        r"swapw|last|lastp|next|prev|show|showb|showw|start|info|clearhist|copy|"
                        r"setb|deleteb|wait|joinp|breakp|linkw|unlinkw|"
                        r"rotatew|findw|suspendc|movew|movep)\Z")
# tmux key names that type a line ending, or a character; other key names
# (Up, C-c, BSpace, Tab ...) edit, recall or complete, so what runs can't be known.
_KEY_TEXT = {"enter": "\n", "c-m": "\n", "c-j": "\n", "kpenter": "\n", "^m": "\n", "^j": "\n",
             "space": " "}
_KEY_NAME = re.compile(r"(?i)(?:[CMS]-)+\S+|\^.|f\d+|kp\w+|up|down|left|right|bspace|btab|dc|"
                       r"delete|end|escape|home|ic|insert|npage|pagedown|pgdn|ppage|pageup|pgup|"
                       r"any|tab|mouse\w*|wheel\w*", re.S)
FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
XARGS = Opts("0oprtx", "adEILnPs", "eil", "arg-file= delimiter= eof=? replace=? max-lines=? max-args= "
             "max-procs= max-chars= null open-tty interactive no-run-if-empty verbose exit "
             "show-limits process-slot-var= " + _COMMON)


_NESTERS = frozenset(SHELLS) | frozenset(C_TAKES_TEXT) | {"su", "runuser", "eval", "screen", "tmux", "xargs", "find"}


# Programs that run their operands (or a shell) but have no table here: "unknown", never "nothing".
LAUNCHERS = frozenset("doas pkexec chroot taskset chrt numactl unshare nsenter setpriv strace ltrace fakeroot "
                      "unbuffer sshpass gosu su-exec bwrap faketime chpst runas "
                      "chronic ifne flock watch parallel xvfb-run systemd-run ssh docker kubectl xterm".split())


# Builtins that evaluate an operand as arithmetic or a subscript, running a substitution in it.
_SUBSCRIPTED = frozenset({"let", "read", "declare", "typeset", "local", "readonly", "export", "unset",
                          "printf", "test", "[", "mapfile", "readarray", "getopts"})


@dataclass(frozen=True)
class GitArgs:
    options: tuple       # global options as (name, value or None), in order
    subcommand: str | None   # None: none given, or ok is False
    rest: tuple          # the subcommand's own arguments (unread words if not ok)
    ok: bool             # False: a global option this table doesn't list (or a missing value),
    #                      any `-c`/`--config-env` key off a short inert allowlist, a `<helper>::` word, or a non-literal subcommand
    unknown: str | None = None   # that option or word, as written; None when ok
    runs_command: bool = False   # True (with ok False) when a `-c`/`--config-env` key can run a command
    #                              (`alias.x=!cmd`, core.pager, core.fsmonitor ...). `subcommand` is the
    #                              name as written, before alias resolution.


_GIT_FLAGS = frozenset("""-p --paginate -P --no-pager --no-replace-objects --no-lazy-fetch --no-optional-locks
    --no-advice --bare --literal-pathspecs --glob-pathspecs --noglob-pathspecs --icase-pathspecs -v --version
    -h --help --html-path --man-path --info-path""".split())
_GIT_VALUES = frozenset("-C -c --git-dir --work-tree --namespace --super-prefix --config-env --attr-source".split())


# INVERTED allowlist (r11b): a `-c`/`--config-env` key is unsafe unless it is listed here, because
# a denylist of command-running keys always lags git (trailer.*.command, remote.*.uploadpack,
# core.alternateRefsCommand, *.path, ext:: urls ...). Lower-cased; a trailing `.` entry is a whole
# section. Only keys that never run a command or load a path.
_GIT_INERT = frozenset("""
    user.name user.email user.useconfigonly
    color. advice. status.
    core.quotepath core.autocrlf core.filemode core.whitespace core.abbrev core.ignorecase core.safecrlf core.eol
    push.default push.followtags
    init.defaultbranch pull.rebase pull.ff merge.ff
    log.date log.decorate log.abbrevcommit
    diff.algorithm diff.renames diff.context
    branch.sort tag.sort fetch.prune""".split())


def _git_cmd_key(key, value):
    """True unless `key` is on the inert allowlist. `alias.*` is not listed, so it is unsafe: it renames the subcommand."""
    parts = key.lower().split(".")
    if len(parts) < 2:
        return True
    k = parts[0] + "." + parts[-1]
    return not (len(parts) == 2 and (k in _GIT_INERT or parts[0] + "." in _GIT_INERT))


_GIT_TRANSPORT = re.compile(r"(?:^|=)[A-Za-z0-9+.-]+::")    # `ext::sh -c x`, `<helper>::addr`


def _unquoted_expansion(raw):
    """True if `$`, a backtick, a glob character or a `{..}` sits outside quotes: word splitting,
    globbing or brace expansion could add options.

    Scans left to right like the shell: `\\x` is one quoted character, `'..'` and `$'..'`
    and `".."` are skipped, anything else with `$` or a backtick counts. An unterminated
    quote counts too."""
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\":
            i += 2
        elif c in "'\"" or (c == "$" and raw[i + 1:i + 2] == "'"):
            q = "'" if c == "$" else c
            i += 2 if c == "$" else 1
            while i < n and raw[i] != q:
                i += 2 if raw[i] == "\\" and (q == '"' or c == "$") else 1
            if i >= n:
                return True
            i += 1
        elif c in "$`*?[":
            return True
        elif c == "{" and "}" in raw[i:]:        # brace expansion makes several words
            return True
        else:
            i += 1
    return False


def git_args(words):
    """Split `git [global options] subcommand args` (words start with git; strings
    or Word objects; a string is taken as the already-unquoted text, so its quote characters
    count as literal: pass Word objects when quoting matters). Git reads its global options exactly: `-C path` and `-c name=val` are two
    words, `-C/x`, `-ccore.x=1` and `-pP` are errors, long options are not
    abbreviated; `--x=v` or `--x v` for the valued ones, `--exec-path[=v]` and
    `--list-cmds=v` only attached. Anything else before the subcommand is ok False,
    as is a non-literal word there, or a `-c`/`--config-env` key that runs a command
    (runs_command True)."""
    lits = [getattr(w, "literal", True) for w in words[1:]]
    args, opts, i = [getattr(w, "text", w) for w in words[1:]], [], 0
    raws = [getattr(w, "raw", getattr(w, "text", w)) for w in words[1:]]
    while i < len(args):
        a = args[i]
        name, eq, val = a.partition("=") if a.startswith("--") else (a, "", "")
        if a in _GIT_FLAGS:
            opts.append((a, None))
        elif a in _GIT_VALUES and i + 1 < len(args):
            opts.append((a, args[i + 1]))
            if a not in ("-c", "--config-env") and _unquoted_expansion(raws[i + 1]):
                return GitArgs(tuple(opts[:-1]), None, tuple(args[i:]), False, args[i + 1], True)
            if a in ("-c", "--config-env"):
                k, e, v = args[i + 1].partition("=")
                if _git_cmd_key(k, v if e and a == "-c" else None) or _nonlit(k):
                    return GitArgs(tuple(opts[:-1]), None, tuple(args[i:]), False, args[i + 1], True)
            i += 1
        elif eq and name == "--exec-path":               # git runs ./git-<cmd> from that directory
            return GitArgs(tuple(opts), None, tuple(args[i:]), False, a, True)
        elif eq and name in _GIT_VALUES:
            opts.append((name, val))
            if name != "--config-env" and _unquoted_expansion(raws[i]):
                return GitArgs(tuple(opts[:-1]), None, tuple(args[i:]), False, a, True)
            if name == "--config-env":
                if _git_cmd_key(val.partition("=")[0], None):                # the value is an env var name: unreadable
                    return GitArgs(tuple(opts[:-1]), None, tuple(args[i:]), False, val, True)
        elif a == "--exec-path" or (eq and name in ("--exec-path", "--list-cmds")):
            opts.append((name, val if eq else None))
        elif a.startswith("-"):
            return GitArgs(tuple(opts), None, tuple(args[i:]), False, a)
        elif _nonlit(a, lits[i]):
            return GitArgs(tuple(opts), None, tuple(args[i:]), False, a)
        else:
            bad = next((w for w in args[i + 1:] if _GIT_TRANSPORT.search(w)), None)
            if bad is not None:                          # a transport helper url runs a program
                return GitArgs(tuple(opts), None, tuple(args[i:]), False, bad, True)
            return GitArgs(tuple(opts), a, tuple(args[i + 1:]), True)
        i += 1
    return GitArgs(tuple(opts), None, (), True)


def nested(words, unwrap_first=True):
    """What this command runs in turn. [] when nothing. With `unwrap_first`
    (default) sudo, env and the like are peeled first: `sudo bash -c x` gives
    the bash -c text; a wrapper unwrap() stops at (query, edit, unsized option,
    untrusted path, too deep) gives one `unknown` for it."""
    if unwrap_first and words:
        u = unwrap(words)
        if u.stopped:
            return [Nested((u.wrappers or (os.path.basename(words[0]),))[-1], "unknown", words=u.words)]
        words = u.words
    if not words:
        return []
    if _nonlit(words[0]):                                # `$S x`: runs whatever the expansion makes it
        return [Nested(os.path.basename(words[0]), "unknown", words=tuple(words[1:]))]
    prog, args = os.path.basename(words[0]), list(words[1:])
    if prog in _NESTERS and not _trusted(words[0]):
        # `./bash` or `/tmp/tmux` may be anything: the same gate unwrap() puts
        # on `./sudo`, so we don't claim to know what it does with its text.
        return [Nested(prog, "unknown", words=tuple(args))]
    if prog == "trap":                                   # trap 'text' SIGNAL runs the text later
        ops = args[1:] if args[:1] == ["--"] else args
        return [Nested("trap", "text", text=ops[0])] if len(ops) > 1 and ops[0] not in ("-", "-l", "-p") else []
    if prog in LAUNCHERS or prog in (".", "source"):    # `source <(cmd)`: cmd is found as a procsub; a file is unknown
        return [Nested(prog, "unknown", words=tuple(args))]
    if prog in _SUBSCRIPTED and any("[" in a and ("$(" in a or "`" in a) for a in args):
        return [Nested(prog, "unknown", words=tuple(args))]   # a[$(cmd)]: arithmetic expands it
    if prog in SHELLS:
        return _shell(prog, args)
    if prog in C_TAKES_TEXT:
        opts, rest, ok = getopt(args, C_TAKES_TEXT[prog])
        texts = [v for o, v in opts if o in ("-c", "--command")]
        if ok and prog == "fish":                        # every -c and -C runs, init commands first
            texts = [v for o, v in opts if o in ("-C", "--init-command")] + texts
            if texts:
                return [Nested(prog, "text", text=t) for t in texts]
        if ok and texts:
            # script also writes the files its options name (`script -qc make out.log`)
            files = tuple(v for o, v in opts if prog == "script" and o in ("-O", "-B", "-I", "-T", "-E", "-m")) + (tuple(rest) if prog == "script" else ())
            return [Nested(prog, "text", text=texts[-1], words=files)]
        return [Nested(prog, "unknown", words=tuple(rest))]   # a script file, or stdin
    if prog in ("su", "runuser"):
        opts, rest, ok = getopt(args, SU, permute=True)  # `su root -c text`
        texts = [v for o, v in opts if o in ("-c", "--command", "--session-command")]
        if not ok or any(_NONLIT.search(a) for a in args):   # `-u $U` can split into more options
            return [Nested(prog, "unknown", words=tuple(args))]
        if texts:
            return [Nested(prog, "text", text=texts[-1])]
        if any(o in ("-u", "--user") for o, _ in opts) and rest:   # runuser -u USER COMMAND
            return [Nested(prog, "words", words=tuple(rest))]
        extra = rest[1:] if rest[:1] != ["-"] else rest[2:]    # after `su [-] USER`: arguments for its shell
        return [Nested(prog, "unknown", words=tuple(extra))]   # no command: its shell reads stdin
    if prog == "eval":
        args = args[1:] if args[:1] == ["--"] else args
        return [Nested("eval", "text", text=" ".join(args))] if args else []
    if prog == "screen":
        return _screen(args)
    if prog == "tmux":
        opts, rest, ok = getopt(args, TMUX)
        if not ok:
            return [Nested("tmux", "unknown", words=tuple(args))]
        out = [Nested("tmux", "text", text=v) for o, v in opts if o == "-c"]
        if any(o == "-f" for o, _ in opts):                  # its config file runs commands
            out.append(Nested("tmux", "unknown", words=tuple(rest)))
        for cmd in _tmux_split(rest):
            out += _tmux(cmd)
        return out
    if prog == "xargs":
        opts, rest, ok = getopt(args, XARGS)
        # It appends words read from stdin, so it stays "unknown"; `words` is
        # the command it runs (echo by default), or () when that can't be sized.
        return [Nested("xargs", "unknown", words=tuple(rest or ["echo"]) if ok else ())]
    if prog == "find":
        out = []
        for k, a in enumerate(args):
            if a in FIND_EXEC:
                end = next((j for j in range(k + 1, len(args)) if args[j] in (";", "+")), len(args))
                out.append(Nested("find " + a, "unknown", words=tuple(args[k + 1:end])))
        return out
    return []


def _shell(prog, args):
    """bash and its kin: `-c` is a flag, and the text is the first operand
    after all the options (`bash -c -e 'cmd'` runs cmd)."""
    saw_c, k = False, 0
    while k < len(args):
        a = args[k]
        if a in ("--", "-"):
            k += 1
            break
        if a.startswith("--"):
            k += 2 if a in SHELL_VALUE_LONGS else 1
            continue
        if a[:1] not in ("-", "+") or a == "+":
            break
        k += 1
        for ch in a[1:]:
            if ch == "c" and a[0] == "-":           # `+c` is no -c
                saw_c = True
            elif ch in SHELL_VALUE_OPTS:
                k += 1                               # `-oc pipefail text`: its value is the next argument, and -c still counts
    if saw_c:
        return [Nested(prog, "text", text=args[k])] if k < len(args) else []
    return [Nested(prog, "unknown", words=tuple(args[k:]))]   # a script file, or stdin


def _screen(args):
    head = list(itertools.takewhile(lambda a: a.startswith("-"), args))
    if any(a in ("-ls", "-list", "-wipe") for a in head):
        return []
    opts, rest, ok = getopt(args, SCREEN)
    flags = {o for o, _ in opts}
    if not ok or flags & {"-X", "-c"}:               # -X: a command for a running session; -c: an rc file
        return [Nested("screen", "unknown", words=tuple(args))]
    if "-R" in flags:                                # reattaches if it can, else runs them
        return [Nested("screen", "unknown", words=tuple(rest))] if rest else []
    if flags & {"-r", "-x", "-Q"}:
        return []                                    # reattaching or querying, not starting
    return [Nested("screen", "words", words=tuple(rest))] if rest else []


def _tmux_split(args):
    """tmux's argv split into commands at `;` (a word of its own, or ending
    one), as tmux does; `\\;` is a literal semicolon."""
    cmds, cur = [], []
    for a in args:
        if a == ";":
            cmds.append(cur)
            cur = []
        elif a.endswith("\\;"):
            cur.append(a[:-2] + ";")
        elif a.endswith(";"):
            cmds.append(cur + [a[:-1]])
            cur = []
        else:
            cur.append(a)
    return [c for c in cmds + [cur] if c]


def _tmux(cmd):
    sub, rest = cmd[0], cmd[1:]
    if any("#(" in a for a in cmd):
        return [Nested("tmux", "unknown", words=tuple(cmd))]   # a format running a command
    if TMUX_QUIET.match(sub):
        return []
    hits = [n for n, (alias, _, _) in TMUX_CMDS.items() if sub in (n, alias)]
    hits = hits or [n for n in TMUX_CMDS if n.startswith(sub)]   # tmux takes a unique prefix
    if not hits:
        # bind-key, set-hook, set-option, source-file ...: they store or
        # load commands that run later.
        return [Nested("tmux", "unknown", words=tuple(cmd))]
    if len(hits) > 1:
        return [Nested("tmux", "unknown", words=tuple(cmd))]
    _, spec, role = TMUX_CMDS[hits[0]]
    opts, rest, ok = getopt(rest, spec)
    flags = {o for o, _ in opts}
    if not ok or role == "unknown" or (hits[0] == "run-shell" and "-C" in flags):
        return [Nested("tmux", "unknown", words=tuple(cmd))]
    if role == "if":
        out = [Nested("tmux", "text", text=rest[0])] if rest else []
        return out + ([Nested("tmux", "unknown", words=tuple(rest[1:]))] if rest[1:] else [])
    if role == "keys":
        return _send_keys(rest, flags)
    if len(rest) == 1:
        return [Nested("tmux", "text", text=rest[0])]
    return [Nested("tmux", "words", words=tuple(rest))] if rest else []


def _send_keys(keys, flags):
    """The lines typed by `send-keys`, run when a line ending follows them.
    Keys are typed one after another, with nothing between them."""
    if "-X" in flags and keys[:1] and re.match(r"(?:copy-)?pipe", keys[0]):
        # copy-pipe, copy-pipe-and-cancel, pipe-no-clear ...: they run a shell command
        return [Nested("tmux send-keys", "text", text=keys[1])] if keys[1:] else []
    if flags & {"-X", "-M"}:
        return []                                    # other copy-mode commands, mouse events
    if flags & {"-H", "-K", "-F"}:
        return [Nested("tmux send-keys", "unknown", words=tuple(keys))]
    typed = []
    for key in keys:
        if "-l" in flags:
            typed.append(key)
        elif key.lower() in _KEY_TEXT:
            typed.append(_KEY_TEXT[key.lower()])
        elif _KEY_NAME.fullmatch(key) and len(key) > 1:
            return [Nested("tmux send-keys", "unknown", words=tuple(keys))]
        else:
            typed.append(key)
    text = "".join(typed).replace("\r", "\n")
    end = text.rfind("\n")
    if "!" in text[:end]:                            # history expansion: not what was typed
        return [Nested("tmux send-keys", "unknown", words=tuple(keys))]
    if end != -1 and not text[:end].strip():
        # A bare Enter submits whatever an earlier send-keys typed.
        return [Nested("tmux send-keys", "unknown", words=tuple(keys))]
    return [Nested("tmux send-keys", "text", text=text[:end])] if end != -1 else []

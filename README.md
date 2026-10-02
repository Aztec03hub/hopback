# claude-sessions

Browse every Claude Code session on your machine, from any directory, and resume one.

`claude --resume` only shows sessions for the directory you're in. `claude-sessions` reads the session store directly (`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`), lists every session newest first, and when you pick one it `cd`s into **that session's own working directory** and runs `claude --resume <id>` there.

## Features

- A full-screen terminal picker built on [Textual](https://textual.textualize.io/), with fuzzy search, alternating row colours, mouse hover highlighting and click support
- Columns: last active ("3h ago, 2:15 PM", "a day ago, …", then "Sep 4"), role, name, directory, size (KB/MB)
- Session names, in order of preference: your `/rename` title, then the generated title, then the agent name
- A preview pane with the directory, git branch, model, cost, lines changed, the last prompt, Claude's last message, and the opening prompt
- Agent sessions (agent-team teammates and Agent SDK runs) are hidden by default; press `CTRL-T` or click the toggle line to show them. Team leads are tagged `lead`.
- Sessions started in `/tmp` and sessions with no messages are hidden by default
- Plain-list mode for scripts and pipes

## Platform support

**Built for and tested only on Claude Code running inside WSL (Ubuntu on Windows).** Other Linux distributions should work the same way, since it's plain Python with a bash installer. That has not been tested.

- **macOS:** not tested and not officially supported. It may work, but nobody has tried it.
- **Native Windows** (PowerShell/cmd): **not supported.** The installer is bash, and the time formatting and `exec`-based resume are POSIX-only. Run Claude Code and this tool inside WSL instead.

## Install

Requires Python 3.9+ with the `venv` module (on Ubuntu, `sudo apt install python3-venv`) and Claude Code (`claude` on your PATH).

```bash
git clone https://github.com/Aztec03hub/claude-sessions.git
cd claude-sessions
./install.sh
```

`install.sh` creates a private venv at `~/.local/share/claude-sessions/venv`, installs Textual into it, and writes the `claude-sessions` command to `~/.local/bin`. You can override those locations with `XDG_DATA_HOME` and `BIN_DIR`. If that directory isn't already on your `PATH`, the installer appends an `export PATH=...` line to `~/.bashrc` (and to `~/.zshrc` if you have one). Open a new shell, or run `source ~/.bashrc`, and `claude-sessions` works from anywhere. Running the installer again is safe: it upgrades in place and never duplicates the PATH line.

Manual alternative: run `pip install textual`, then copy `claude-sessions` anywhere on your PATH.

## Usage

```
claude-sessions                 picker
claude-sessions stremio         picker, pre-filtered
claude-sessions -y              resume with --dangerously-skip-permissions
claude-sessions -l              plain list, no picker (also automatic when piped)
claude-sessions -n 50 / -a      how many to load (default 300) / load all
claude-sessions -d              only sessions from the current directory
claude-sessions -t              include agent sessions
claude-sessions -s              include /tmp sessions
claude-sessions -e              include sessions with no messages
claude-sessions --deep          read whole files (slower, finds early-only titles)
claude-sessions --id <prefix>   print the full id for a prefix, then exit
claude-sessions --print-cd      print the cd && claude command instead of running it
claude-sessions --doctor        explain why the picker did or didn't appear
```

### Picker keys

| Key | Action |
|---|---|
| type | fuzzy search over name, path and id |
| ↑/↓, `CTRL-J`/`CTRL-K`, mouse wheel | move |
| `ENTER` / click | resume |
| `CTRL-Y` | resume with `--dangerously-skip-permissions` |
| `CTRL-T` / click the toggle line | show or hide agent sessions |
| `CTRL-/` | toggle the preview pane |
| `CTRL-U` | clear the search |
| `ESC` | quit |

### Markers

- `·` means you named the session with `/rename`
- `?` means the directory was inferred from the store's folder name, which is lossy (`/` and `_` both encode as `-`). The tool never `cd`s into an inferred directory that doesn't exist.

## Why Textual (and not fzf)

The first version was a plain Python list printer. Next it became an fzf front end, and then it was rewritten in Textual. The switch to Textual happened because fzf only applies its focus colours where the row text doesn't set its own colours. That makes alternating row stripes and a uniformly highlighted focused row mutually exclusive, and fzf has no mouse-motion events, so hover highlighting is impossible. Textual's CSS cascade lets the focus and hover rules override the stripe rule, and it allows full colour theming. Everything else matches the original fzf behaviour.

## How it works

- Files are read **backwards** from the tail (256 KB by default) to find the newest title and cwd cheaply. Titles are rewritten during a session and the last one wins, and long sessions can run to hundreds of MB.
- The session's `entrypoint` (`cli` vs `sdk-*`) separates sessions you started yourself from Agent SDK runs.
- Team leads come from `~/.claude/teams/*/config.json` (`leadSessionId`), so only teams that are currently active are detected.
- Subagent transcripts (`<session>/subagents/*.jsonl`) are never listed.

It is read-only: nothing in `~/.claude` is ever modified.

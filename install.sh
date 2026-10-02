#!/usr/bin/env bash
# Installs claude-sessions into its own venv and puts it on PATH.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
venv="${XDG_DATA_HOME:-$HOME/.local/share}/claude-sessions/venv"
bin="${BIN_DIR:-$HOME/.local/bin}"
python3 -m venv "$venv"
"$venv/bin/pip" install -q --upgrade -r "$here/requirements.txt"
mkdir -p "$bin"
sed "1s|.*|#!$venv/bin/python|" "$here/claude-sessions" > "$bin/claude-sessions"
chmod +x "$bin/claude-sessions"
echo "installed $bin/claude-sessions"
# Put $bin on PATH for future shells if it is not there already.
case ":$PATH:" in
  *":$bin:"*) ;;
  *)
    line="export PATH=\"$bin:\$PATH\""
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
      [ -f "$rc" ] || [ "$rc" = "$HOME/.bashrc" ] || continue
      grep -qxF "$line" "$rc" 2>/dev/null || { printf '\n# claude-sessions\n%s\n' "$line" >> "$rc"; echo "added $bin to PATH in $rc"; }
    done
    echo "open a new shell (or: source ~/.bashrc) to use claude-sessions"
    ;;
esac

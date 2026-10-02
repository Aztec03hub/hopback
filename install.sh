#!/usr/bin/env bash
# Installs hopback into its own venv and puts it (and the old claude-sessions
# name) on PATH.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
venv="${XDG_DATA_HOME:-$HOME/.local/share}/hopback/venv"
bin="${BIN_DIR:-$HOME/.local/bin}"
python3 -m venv "$venv"
"$venv/bin/pip" install -q --upgrade "$here"
mkdir -p "$bin"
for cmd in hopback claude-sessions; do
  ln -sf "$venv/bin/$cmd" "$bin/$cmd"
done
echo "installed $bin/hopback (and $bin/claude-sessions)"
# Put $bin on PATH for future shells if it is not there already.
case ":$PATH:" in
  *":$bin:"*) ;;
  *)
    line="export PATH=\"$bin:\$PATH\""
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
      [ -f "$rc" ] || [ "$rc" = "$HOME/.bashrc" ] || continue
      grep -qxF "$line" "$rc" 2>/dev/null || { printf '\n# hopback\n%s\n' "$line" >> "$rc"; echo "added $bin to PATH in $rc"; }
    done
    echo "open a new shell (or: source ~/.bashrc) to use hopback"
    ;;
esac

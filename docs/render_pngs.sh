#!/usr/bin/env bash
# Render every docs/*.svg screenshot to a PNG at its native size with headless
# Chrome (the SVG carries its own width/height after make_screenshots.py).
set -euo pipefail
cd "$(dirname "$0")"
chrome="${CHROME:-$(command -v google-chrome || command -v chromium || command -v chromium-browser)}"
for svg in *.svg; do
  read -r w h < <(python3 -c "import re,sys;v=re.search(r'width=\"(\d+)\" height=\"(\d+)\"',open(sys.argv[1]).read());print(v[1],v[2])" "$svg")
  "$chrome" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --window-size="$w,$h" --screenshot="$PWD/${svg%.svg}.png" "file://$PWD/$svg" 2>/dev/null
  echo "rendered ${svg%.svg}.png ($w x $h)"
done

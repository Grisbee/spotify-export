#!/bin/bash
# Builds "Eksport playlist.app": double-click starts the server in the
# background (no Terminal) and opens the browser; if it's already running,
# it just opens the page. Stop it with the "Zakończ" button in the UI.
#
# Usage: launcher/build_app.sh [destination dir]   (default: dist/)
# PYTHON=/path/to/python3 overrides the interpreter baked into the app.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$HERE")"
# Finder starts apps with a minimal PATH, so the interpreter path is fixed now.
PYTHON="${PYTHON:-$(command -v python3)}"
DEST="${1:-$PROJECT/dist}"
APP="$DEST/Eksport playlist.app"
PORT=8888

if ! "$PYTHON" -c "import requests" 2>/dev/null; then
  echo "$PYTHON nie ma biblioteki requests — uruchom: $PYTHON -m pip install -r requirements.txt" >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Eksport playlist</string>
  <key>CFBundleDisplayName</key><string>Eksport playlist</string>
  <key>CFBundleIdentifier</key><string>local.spotify-export.launcher</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <!-- The launcher exits right after starting the server; no Dock icon needed. -->
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

{
  echo '#!/bin/bash'
  printf 'PROJECT=%q\nPYTHON=%q\nPORT=%q\n' "$PROJECT" "$PYTHON" "$PORT"
  cat <<'LAUNCHER'
URL="http://127.0.0.1:$PORT/"
LOG="$HOME/Library/Logs/spotify-export.log"

# Already running: just bring the page up.
if curl -s -o /dev/null --max-time 2 "$URL"; then
  open "$URL"
  exit 0
fi

cd "$PROJECT" || exit 1
echo "--- $(date '+%Y-%m-%d %H:%M:%S') start" >> "$LOG"
nohup "$PYTHON" -u app.py --port "$PORT" >> "$LOG" 2>&1 &
pid=$!

# A missing .env or a busy port makes app.py exit at once; surface that
# instead of failing silently.
sleep 1.5
if ! kill -0 "$pid" 2>/dev/null; then
  osascript - "$(tail -n 5 "$LOG")" <<'OSA'
on run argv
  display alert "Nie udało się uruchomić eksportu playlist" message (item 1 of argv) as critical
end run
OSA
  exit 1
fi
LAUNCHER
} > "$APP/Contents/MacOS/launcher"
chmod +x "$APP/Contents/MacOS/launcher"

# Make Finder pick up the new icon if the bundle existed before.
touch "$APP"
echo "Zbudowano: $APP"

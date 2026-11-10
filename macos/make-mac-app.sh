#!/bin/bash
# Build "Work Sessions.app" — a Dock-friendly launcher for this repo (macOS only).
#
#   ./macos/make-mac-app.sh                      # -> ~/Applications/Work Sessions.app
#   ./macos/make-mac-app.sh "/Applications"      # or another folder
#
# Clicking the app starts the server in the background (if it isn't running yet) and
# opens the board in your browser. No Terminal window. Re-run this script if you move
# the repo. Stop the server with:  ./macos/stop.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
REPO_LITERAL="$(printf '%q' "$REPO")"
DEST_DIR="${1:-$HOME/Applications}"
NAME="Work Sessions"
APP="$DEST_DIR/$NAME.app"

[ "$(uname)" = "Darwin" ] || { echo "This script builds a macOS app; run it on a Mac."; exit 1; }
mkdir -p "$DEST_DIR"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---- icon: icon.png -> AppIcon.icns (sips and iconutil ship with macOS)
ICONSET="$TMP/AppIcon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$REPO/macos/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d "$REPO/macos/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
if ! iconutil --convert icns "$ICONSET" --output "$TMP/AppIcon.icns"; then
  # Some recent macOS releases reject even valid iconsets. Keep the launcher usable
  # with a single-resolution ICNS rather than producing a broken app bundle.
  echo "iconutil could not create a multi-resolution icon; using a 512px fallback." >&2
  sips -z 512 512 "$REPO/macos/icon.png" --out "$TMP/AppIcon-512.png" >/dev/null
  sips -s format icns "$TMP/AppIcon-512.png" --out "$TMP/AppIcon.icns" >/dev/null
fi

# ---- bundle (built in a temp folder, then swapped in)
B="$TMP/$NAME.app/Contents"
mkdir -p "$B/MacOS" "$B/Resources"
cp "$TMP/AppIcon.icns" "$B/Resources/AppIcon.icns"

cat > "$B/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>local.worksessions.launcher</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# the repo path is baked in; everything else is read at launch
cat > "$B/MacOS/launcher" <<LAUNCHER
#!/bin/bash
REPO=$REPO_LITERAL
LAUNCHER
cat >> "$B/MacOS/launcher" <<'LAUNCHER'
# Finder-launched apps get a minimal PATH, so look for python3 explicitly
PY=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$p" ]; then PY="$p"; break; fi
done
alert() { /usr/bin/osascript -e "display alert \"Work Sessions\" message \"$1\" as warning" >/dev/null; }
[ -n "$PY" ] || { alert "python3 was not found. Install it with: xcode-select --install"; exit 1; }
[ -f "$REPO/app.py" ] || { alert "Can't find the app at $REPO. Re-run macos/make-mac-app.sh from the repo."; exit 1; }

cd "$REPO" || exit 1
mkdir -p data
PORT="$("$PY" -c 'import json,os;p="data/config.json";print(json.load(open(p)).get("port",8765) if os.path.exists(p) else 8765)' 2>/dev/null || echo 8765)"
URL="http://127.0.0.1:$PORT/"

ours() { /usr/bin/curl -sf "${URL}api/state" 2>/dev/null | grep -q '"version"'; }

if ! ours; then
  if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    alert "Port $PORT is used by another program (perhaps another copy of Work Sessions). Stop it, or set a different \"port\" in $REPO/data/config.json."
    exit 1
  fi
  nohup "$PY" "$REPO/app.py" --no-browser >> data/app.log 2>&1 &
  echo $! > data/app.pid
  for _ in $(seq 1 50); do ours && break; sleep 0.2; done
  ours || { alert "The server didn't start. See $REPO/data/app.log"; exit 1; }
fi
/usr/bin/open "$URL"
LAUNCHER
chmod +x "$B/MacOS/launcher"

BACKUP=""
if [ -e "$APP" ]; then
  # Keep the prior bundle outside the temporary directory so the EXIT trap cannot remove it,
  # and not ending in .app so macOS (Launchpad, Spotlight, Dock) doesn't register it as a
  # second "Work Sessions" app. To restore one, rename it back to "Work Sessions.app".
  BACKUP="$REPO/data/previous-builds/$NAME-$(date +%Y%m%d-%H%M%S).app.previous"
  mkdir -p "$(dirname "$BACKUP")"
  mv "$APP" "$BACKUP"
fi
if ! mv "$TMP/$NAME.app" "$APP"; then
  [ -n "$BACKUP" ] && mv "$BACKUP" "$APP"
  exit 1
fi
touch "$APP"   # refresh Finder/Dock icon cache

echo "Built: $APP"
[ -n "$BACKUP" ] && echo "Previous build kept at: $BACKUP"
echo "To pin it: in Finder choose Go → Go to Folder… → $DEST_DIR, then drag \"$NAME\" onto the Dock."
[ "$(uname)" = "Darwin" ] && /usr/bin/open -R "$APP" 2>/dev/null || true

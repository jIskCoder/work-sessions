#!/bin/bash
# Build "Work Sessions.app" — a Dock-friendly launcher for this repo (macOS only).
#
#   ./macos/make-mac-app.sh                      # -> ~/Applications/Work Sessions.app
#   ./macos/make-mac-app.sh "/Applications"      # or another folder
#
# The app is a small "stay-open" macOS app, so it behaves like other apps in the Dock:
#   * opening it starts the server (if it isn't running yet) and shows the board in your
#     browser, bringing an already-open Work Sessions tab to the front;
#   * a dot under the Dock icon means it's running; clicking the icon shows the board again;
#   * Quit (Dock menu or Cmd-Q) stops the server; if the server stops some other way
#     (./macos/stop.sh, a crash), the app notices within 30 seconds and quits too.
# No Terminal window. Re-run this script if you move the repo.
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

# ---- app: a stay-open AppleScript applet (osacompile ships with macOS). Being a real,
# running app is what gives it the Dock dot, Dock-click ("reopen") handling and Quit.
# The shell helper in Resources/launcher.sh does the actual work.
cat > "$TMP/main.applescript" <<'OSA'
on run
	showBoard()
end run

-- clicking the Dock icon (or opening the app again) while it's running
on reopen
	showBoard()
end reopen

on showBoard()
	try
		do shell script helper() & " open"
	on error errMsg number errNum
		display alert "Work Sessions" message errMsg as warning
		quit
	end try
end showBoard

-- every 30 seconds: if the server has stopped some other way, quit so the Dock dot goes away
on idle
	try
		do shell script helper() & " status"
	on error
		quit
	end try
	return 30
end idle

on quit
	try
		do shell script helper() & " stop"
	end try
	continue quit
end quit

on helper()
	return quoted form of ((POSIX path of (path to me)) & "Contents/Resources/launcher.sh")
end helper
OSA
osacompile -s -o "$TMP/$NAME.app" "$TMP/main.applescript"

R="$TMP/$NAME.app/Contents/Resources"
PL="$TMP/$NAME.app/Contents/Info.plist"
cp "$TMP/AppIcon.icns" "$R/applet.icns"
rm -f "$R/Assets.car"   # newer applets carry an icon catalog that would override applet.icns
plutil -remove CFBundleIconName "$PL" 2>/dev/null || true
plutil -replace CFBundleIconFile -string "applet" "$PL"
plutil -replace CFBundleName -string "$NAME" "$PL"
plutil -replace CFBundleDisplayName -string "$NAME" "$PL"
plutil -replace CFBundleIdentifier -string "local.worksessions.launcher" "$PL"
plutil -replace CFBundleShortVersionString -string "2.0" "$PL"
plutil -replace NSAppleEventsUsageDescription -string \
  "Work Sessions brings its open browser tab to the front instead of opening a new one." "$PL"

# the repo path is baked in; everything else is read at launch
cat > "$R/launcher.sh" <<LAUNCHER
#!/bin/bash
REPO=$REPO_LITERAL
LAUNCHER
cat >> "$R/launcher.sh" <<'LAUNCHER'
# Helper for Work Sessions.app:  launcher.sh open | status | stop
CMD="${1:-open}"
# Finder-launched apps get a minimal PATH, so look for python3 explicitly
PY=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$p" ]; then PY="$p"; break; fi
done
fail() { echo "$1" >&2; exit 1; }   # the app shows this message in an alert
[ -n "$PY" ] || fail "python3 was not found. Install it with: xcode-select --install"
[ -f "$REPO/app.py" ] || fail "Can't find the app at $REPO. Re-run macos/make-mac-app.sh from the repo."

cd "$REPO" || exit 1
mkdir -p data
PORT="$("$PY" -c 'import json,os;p="data/config.json";print(json.load(open(p)).get("port",8765) if os.path.exists(p) else 8765)' 2>/dev/null || echo 8765)"
URL="http://127.0.0.1:$PORT/"

ours() { /usr/bin/curl -sf "${URL}api/state" 2>/dev/null | grep -q '"version"'; }

case "$CMD" in
  status) ours; exit $? ;;
  stop)
    # stop.sh only stops a server this app started (it checks the PID file)
    if [ -x "$REPO/macos/stop.sh" ]; then "$REPO/macos/stop.sh" >/dev/null 2>&1 || true; fi
    exit 0 ;;
  open) ;;
  *) fail "usage: launcher.sh open|status|stop" ;;
esac

if ! ours; then
  if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "Port $PORT is used by another program (perhaps another copy of Work Sessions). Stop it, or set a different \"port\" in $REPO/data/config.json."
  fi
  nohup "$PY" "$REPO/app.py" --no-browser >> data/app.log 2>&1 &
  echo $! > data/app.pid
  for _ in $(seq 1 50); do ours && break; sleep 0.2; done
  ours || fail "The server didn't start. See $REPO/data/app.log"
fi

# Bring an already-open Work Sessions tab to the front instead of opening another one.
# Safari and Chromium-based browsers can be asked over AppleScript (the first time, macOS
# asks whether "Work Sessions" may control the browser). Otherwise a new tab is opened.
focus_tab() {
  local app="$1" script
  /usr/bin/pgrep -x "$app" >/dev/null 2>&1 || return 1
  if [ "$app" = "Safari" ]; then
    script='on run argv
  set p1 to item 1 of argv
  set p2 to item 2 of argv
  tell application "Safari"
    repeat with w in windows
      repeat with t in tabs of w
        set u to URL of t
        if u is not missing value and (u starts with p1 or u starts with p2) then
          set current tab of w to t
          set index of w to 1
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  return ""
end run'
  else
    script="on run argv
  set p1 to item 1 of argv
  set p2 to item 2 of argv
  tell application \"$app\"
    repeat with w in windows
      set i to 0
      repeat with t in tabs of w
        set i to i + 1
        set u to URL of t
        if u starts with p1 or u starts with p2 then
          set active tab index of w to i
          set index of w to 1
          activate
          return \"ok\"
        end if
      end repeat
    end repeat
  end tell
  return \"\"
end run"
  fi
  [ "$(/usr/bin/osascript -e "$script" "http://127.0.0.1:$PORT/" "http://localhost:$PORT/" 2>/dev/null)" = "ok" ]
}
for browser in "Safari" "Google Chrome" "Microsoft Edge" "Brave Browser" "Chromium" "Vivaldi"; do
  focus_tab "$browser" && exit 0
done
/usr/bin/open "$URL"
LAUNCHER
chmod +x "$R/launcher.sh"

# changing the bundle invalidates osacompile's signature; re-sign it (ad hoc, local only)
codesign --force --deep --sign - "$TMP/$NAME.app" >/dev/null 2>&1 \
  || echo "Note: couldn't re-sign the app; it should still run." >&2

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

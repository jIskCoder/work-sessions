#!/bin/bash
# macOS: double-click to start. Close this Terminal window (or press Ctrl+C) to stop.
cd "$(dirname "$0")"
PORT=$(python3 -c "import json,os;p='data/config.json';print(json.load(open(p)).get('port',8765) if os.path.exists(p) else 8765)")
if curl -s -o /dev/null "http://127.0.0.1:$PORT/api/state"; then
  open "http://127.0.0.1:$PORT/"     # already running
else
  python3 app.py
fi

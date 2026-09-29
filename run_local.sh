#!/bin/bash
# Run the demo on this computer only. Open http://127.0.0.1:4177/mobile/
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Run ./setup.sh first."; exit 1; }
grep -q '^GOOGLE_API_KEY=replace-me' .env 2>/dev/null && echo "Warning: .env still has the placeholder API key."
echo "Open http://127.0.0.1:4177/mobile/  (Ctrl-C to stop)"
exec .venv/bin/python -m uvicorn live_demo.server:app --host 127.0.0.1 --port 4177

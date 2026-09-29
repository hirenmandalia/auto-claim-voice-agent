#!/bin/bash
# One-time setup: virtualenv, dependencies, and a .env file for your API key.
set -e
cd "$(dirname "$0")"

PY=""
for candidate in python3.12 python3.13 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version=$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    case "$version" in 3.11|3.12|3.13) PY="$candidate"; break ;; esac
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11, 3.12 or 3.13 is required. On a Mac: brew install python@3.12"
  exit 1
fi

echo "Using $PY ($($PY --version))"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Created .env. Open it and replace 'replace-me' with your Google API key."
  echo "Get one at https://aistudio.google.com/apikey"
else
  echo ".env already exists, leaving it alone."
fi
echo
echo "Setup complete. Next: ./run_local.sh"

#!/usr/bin/env bash
# Stock Watcher site probe launcher (macOS / Linux).
# First run: creates .venv here, installs the backend's requirements and patchright's Chromium.
# Then forwards every argument to probe.py, e.g.  ./run.sh serve   ./run.sh sweep --only target
#   ./run.sh discover --write   (find current product URLs, update sites.json)   ./run.sh sweep --discover
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQS="$HERE/../../backend/requirements.txt"
VENV="$HERE/.venv"
VPY="$VENV/bin/python"

find_python() {
  local c
  for c in "${PYTHON:-}" python3.13 python3.12 python3.11 python3 python; do
    [ -n "$c" ] || continue
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}

if [ ! -x "$VPY" ]; then
  PY="$(find_python)" || {
    echo "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/ (macOS: 'brew install python@3.12')." >&2
    exit 1
  }
  echo "Creating virtual environment in $VENV ..."
  "$PY" -m venv "$VENV"
fi

# Reinstall only when backend/requirements.txt changed.
STAMP="$VENV/.requirements.cksum"
WANT="$(cksum < "$REQS")"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$WANT" ]; then
  echo "Installing backend requirements (one time, ~1-2 minutes) ..."
  "$VPY" -m pip install --disable-pip-version-check -q --upgrade pip
  "$VPY" -m pip install --disable-pip-version-check -q -r "$REQS"
  echo "$WANT" > "$STAMP"
  rm -f "$VENV/.playwright-ok"
fi

if [ ! -f "$VENV/.playwright-ok" ]; then
  # patchright (the stealthier Playwright the checker prefers) and playwright are pinned to the
  # same release, so this one Chromium serves both.
  ENGINE=patchright
  "$VPY" -c 'import patchright' 2>/dev/null || ENGINE=playwright
  echo "Installing the Chromium used for bot-protected pages ($ENGINE, one time) ..."
  if "$VPY" -m "$ENGINE" install chromium; then
    touch "$VENV/.playwright-ok"
  else
    echo "WARNING: Chromium install failed; browser fallback will not work." >&2
    echo "  Linux may need: sudo $VPY -m $ENGINE install-deps chromium" >&2
    echo "  With Google Chrome installed you can use --chrome instead; or run with --no-browser." >&2
  fi
fi

exec "$VPY" "$HERE/probe.py" "$@"

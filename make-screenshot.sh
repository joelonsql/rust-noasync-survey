#!/usr/bin/env bash
# Regenerate docs/dashboard.png from the live dashboard.
# Starts the dashboard if it isn't already up (and stops it again afterwards),
# renders the ?static one-shot view, and captures it with headless Chrome.
#   ./make-screenshot.sh [output.png]
set -euo pipefail
SURVEY="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$SURVEY/docs/dashboard.png}"
PORT=8787
URL="http://127.0.0.1:$PORT/?static=1"

# locate a headless browser
CHROME=""
for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         "/Applications/Chromium.app/Contents/MacOS/Chromium" \
         "$(command -v chromium || true)" "$(command -v chromium-browser || true)" \
         "$(command -v google-chrome || true)"; do
  [ -n "$c" ] && [ -x "$c" ] && { CHROME="$c"; break; }
done
[ -n "$CHROME" ] || { echo "no Chrome/Chromium found" >&2; exit 1; }

# ensure the dashboard is up; start it ourselves if needed
STARTED=""
if ! curl -sf -o /dev/null "http://127.0.0.1:$PORT/?static=1" 2>/dev/null; then
  echo "starting dashboard…"
  ( ulimit -n 4096; python3 "$SURVEY/dashboard.py" >"$SURVEY/logs/dashboard.log" 2>&1 & )
  STARTED=1
  for _ in $(seq 1 20); do
    curl -sf -o /dev/null "http://127.0.0.1:$PORT/?static=1" 2>/dev/null && break
    sleep 0.5
  done
fi

mkdir -p "$(dirname "$OUT")"
echo "capturing $URL -> $OUT"
timeout 40 "$CHROME" --headless=new --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 --window-size=1360,2500 --virtual-time-budget=5000 \
  --screenshot="$OUT" "$URL" >/dev/null 2>&1 || true

# stop the dashboard only if we started it
if [ -n "$STARTED" ]; then
  lsof -ti "tcp:$PORT" 2>/dev/null | xargs kill 2>/dev/null || true
fi

if [ -s "$OUT" ]; then
  echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
else
  echo "screenshot failed" >&2; exit 1
fi

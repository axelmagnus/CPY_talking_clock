#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="/Volumes/CIRCUITPY"
DST_DIR="/Users/axelmansson/Documents/GitHub/CPY talking clock"
FILES=("code.py" "weather_wmo_lookup.py")

sync_once() {
  for f in "${FILES[@]}"; do
    if [[ -f "$SRC_DIR/$f" ]]; then
      if [[ ! -f "$DST_DIR/$f" ]] || ! cmp -s "$SRC_DIR/$f" "$DST_DIR/$f"; then
        cp -f "$SRC_DIR/$f" "$DST_DIR/$f"
        echo "[$(date +%H:%M:%S)] synced $f"
      fi
    fi
  done
}

echo "Watching $SRC_DIR -> $DST_DIR"
echo "Press Ctrl+C to stop"

while true; do
  sync_once
  sleep 2
done

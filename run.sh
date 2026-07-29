#!/usr/bin/env bash
# run.sh — wrapper that fixes gtk4-layer-shell linking order on Arch.
# All state stays inside this project folder.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
export LD_PRELOAD="/usr/lib/libgtk4-layer-shell.so${LD_PRELOAD:+:$LD_PRELOAD}"

exec "$PROJECT_ROOT/.venv/bin/python3" "$PROJECT_ROOT/translate_popup.py" "$@"

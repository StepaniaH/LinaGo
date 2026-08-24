#!/usr/bin/env bash
# run.sh — bootstrap a venv and launch LinaGo from a checkout.
#
# gtk4-layer-shell is preloaded where the library exists because some
# distros ship the GI binding without linking it into PyGObject's
# lookup path.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
    "$PYTHON" -m venv --system-site-packages .venv
fi
. .venv/bin/activate

if [ -e /usr/lib/libgtk4-layer-shell.so ]; then
    export LD_PRELOAD="/usr/lib/libgtk4-layer-shell.so${LD_PRELOAD:+:$LD_PRELOAD}"
fi

exec python -m linago "$@"

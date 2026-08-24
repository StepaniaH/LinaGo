#!/usr/bin/env bash
# run.sh — bootstrap a venv and launch LinaGo from a checkout.
#
# The project's Python dependencies (requests, tomlkit) are installed
# into the venv on first run; system packages keep providing PyGObject
# and the GTK stack via --system-site-packages. gtk4-layer-shell is
# preloaded where the library exists because some distros ship the GI
# binding without linking it into PyGObject's lookup path.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
    "$PYTHON" -m venv --system-site-packages .venv
fi
. .venv/bin/activate

# Install/refresh the project into the venv whenever the marker is
# missing or stale relative to pyproject.toml.
MARKER=".venv/.linago-installed"
if [ ! -f "$MARKER" ] || [ pyproject.toml -nt "$MARKER" ]; then
    echo "[run.sh] installing Python dependencies (first run)..."
    python -m pip install -q --disable-pip-version-check \
        --upgrade requests tomlkit
    python -m pip install -q --disable-pip-version-check \
        --no-deps -e "$PROJECT_ROOT"
    touch "$MARKER"
fi

if [ -e /usr/lib/libgtk4-layer-shell.so ]; then
    export LD_PRELOAD="/usr/lib/libgtk4-layer-shell.so${LD_PRELOAD:+:$LD_PRELOAD}"
fi

exec python -m linago "$@"

#!/bin/bash
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

TAILWIND_BIN="$HOME/.local/bin/tailwindcss"
if [ ! -f "$TAILWIND_BIN" ]; then
    TAILWIND_BIN="/tmp/tailwindcss"
fi

echo "Compiling Tailwind CSS..."
"$TAILWIND_BIN" --input static/css/input.css --output static/css/tailwind.css --minify "$@"
echo "Tailwind CSS compiled successfully to static/css/tailwind.css"

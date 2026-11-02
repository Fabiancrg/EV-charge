#!/usr/bin/env bash
# build_shiv.sh — Build a self-contained shiv zipapp for Linux/macOS
#
# Usage:
#   ./build_shiv.sh [--output path/to/output.pyz] [--python python3.12]
#
# Prerequisites:
#   - uv (https://github.com/astral-sh/uv) must be installed
#   - A configured .venv in this directory (run: uv sync)
#
# The produced .pyz:
#   - Is directly executable on Linux/macOS (chmod +x is applied automatically)
#   - Can also be run explicitly: python3 ev_charge.pyz [--config ...]
#   - On Windows: python ev_charge.pyz [--config ...] (no shebang needed)
#

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
OUTPUT="${OUTPUT:-dist/ev_charge.pyz}"
PYTHON="${PYTHON:-python3}"
BUILD_DIR="$(mktemp -d)"

# ── Parse arguments ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --output) OUTPUT="$2"; shift 2 ;;
        --python) PYTHON="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

echo "==> Build configuration"
echo "    Python:     $PYTHON ($($PYTHON --version))"
echo "    Output:     $OUTPUT"
echo "    Build dir:  $BUILD_DIR"
echo ""

# Cleanup on exit
trap 'echo ""; echo "Cleaning up build dir: $BUILD_DIR"; rm -rf "$BUILD_DIR"' EXIT

# ── Ensure shiv is available ──────────────────────────────────────────────────
if ! command -v shiv &>/dev/null; then
    echo "==> shiv not found — installing into venv"
    if [[ ! -f ".venv/bin/python" ]]; then
        echo "ERROR: .venv not found. Run 'uv sync' first." >&2
        exit 1
    fi
    uv pip install shiv --python .venv/bin/python
fi

SHIV_CMD="shiv"
# Prefer venv's shiv if available
if [[ -f ".venv/bin/shiv" ]]; then
    SHIV_CMD=".venv/bin/shiv"
fi

# ── Install all runtime dependencies into build dir ───────────────────────────
echo "==> Installing runtime dependencies into $BUILD_DIR"

if command -v uv &>/dev/null; then
    uv pip install \
        --python ".venv/bin/python" \
        --target "$BUILD_DIR" \
        .
elif [[ -f ".venv/bin/pip" ]]; then
    .venv/bin/pip install \
        --target "$BUILD_DIR" \
        .
else
    echo "ERROR: Neither uv nor .venv/bin/pip found." >&2
    exit 1
fi

echo ""
echo "==> Dependency installation complete"
echo "    Installed packages: $(ls "$BUILD_DIR" | wc -l) entries"

# ── Build the shiv archive ────────────────────────────────────────────────────
echo ""
echo "==> Building shiv archive: $OUTPUT"

mkdir -p "$(dirname "$OUTPUT")"

"$SHIV_CMD" \
    --site-packages "$BUILD_DIR" \
    --compressed \
    --python "/usr/bin/env $PYTHON" \
    --entry-point "ev_charge.__main__:main" \
    --output-file "$OUTPUT"

chmod +x "$OUTPUT"

echo ""
echo "==> Build complete!"
echo "    Output: $OUTPUT ($(du -sh "$OUTPUT" | cut -f1))"
echo ""
echo "Usage:"
echo "    $OUTPUT [--config path/to/config.json]"
echo "    $PYTHON $OUTPUT [--config path/to/config.json]"

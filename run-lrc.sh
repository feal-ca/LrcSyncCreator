#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/M/bin/activate"
python3 "$SCRIPT_DIR/lrc_studio/main.py"


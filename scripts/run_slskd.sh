#!/usr/bin/env bash
# Runs slskd in the foreground using the config written by setup_slskd.sh.
# LRC Studio launches this itself when a Soulseek feature (Find on Soulseek,
# Spotify import) needs it and it isn't already running — run it by hand
# only if you want to watch its logs directly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SLSKD_DIR="$ROOT_DIR/slskd-bin"

if [[ ! -x "$SLSKD_DIR/slskd" ]]; then
    echo "slskd isn't installed yet — run ./scripts/setup_slskd.sh first."
    exit 1
fi

cd "$SLSKD_DIR"
exec ./slskd --config ./slskd.yml

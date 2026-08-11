#!/usr/bin/env bash
# Runs slskd in the foreground using the config written by setup_slskd.sh.
# LRC Studio's Soulseek features (Find on Soulseek, Spotify playlist import)
# need this running first.
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

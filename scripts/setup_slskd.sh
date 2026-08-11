#!/usr/bin/env bash
# Downloads and configures slskd (a headless Soulseek daemon) for LRC Studio's
# Soulseek integration. slskd exposes a REST API that lrc_studio/soulseek.py
# talks to — LRC Studio never speaks the Soulseek protocol itself.
#
# Safe to re-run: skips the download if the binary is already present, and
# never overwrites an existing slskd.yml (so your credentials/API key survive).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SLSKD_DIR="$ROOT_DIR/slskd-bin"
VERSION="0.26.0"

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  ASSET="slskd-${VERSION}-linux-x64.zip" ;;
    Linux-aarch64) ASSET="slskd-${VERSION}-linux-arm64.zip" ;;
    Darwin-x86_64) ASSET="slskd-${VERSION}-osx-x64.zip" ;;
    Darwin-arm64)  ASSET="slskd-${VERSION}-osx-arm64.zip" ;;
    *) echo "Unsupported platform: $(uname -s)-$(uname -m)"; exit 1 ;;
esac

mkdir -p "$SLSKD_DIR"

if [[ ! -x "$SLSKD_DIR/slskd" ]]; then
    echo "Downloading slskd ${VERSION} ($ASSET)..."
    curl -sL --fail -o "$SLSKD_DIR/slskd.zip" \
        "https://github.com/slskd/slskd/releases/download/${VERSION}/${ASSET}"
    unzip -q -o "$SLSKD_DIR/slskd.zip" -d "$SLSKD_DIR"
    rm "$SLSKD_DIR/slskd.zip"
    chmod +x "$SLSKD_DIR/slskd"
    echo "slskd binary installed to $SLSKD_DIR/slskd"
else
    echo "slskd binary already present, skipping download."
fi

mkdir -p "$SLSKD_DIR/downloads" "$SLSKD_DIR/incomplete"

CONFIG="$SLSKD_DIR/slskd.yml"
if [[ ! -f "$CONFIG" ]]; then
    API_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
    cat > "$CONFIG" <<EOF
directories:
  downloads: $SLSKD_DIR/downloads
  incomplete: $SLSKD_DIR/incomplete

# Fill in a real Soulseek username/password before first use. Soulseek
# auto-registers a new account the first time you log in with a username
# that isn't already taken — pick something real, not a placeholder.
soulseek:
  username: CHANGE_ME
  password: CHANGE_ME

web:
  port: 5030
  authentication:
    disabled: false
    username: admin
    password: CHANGE_ME_WEB_UI_PASSWORD
    api_keys:
      lrc_studio:
        key: ${API_KEY}
        role: readwrite
        cidr: 127.0.0.1/32,::1/128
EOF
    echo
    echo "Wrote a new config to $CONFIG"
    echo "  -> Edit it and replace the soulseek.username / soulseek.password"
    echo "     placeholders with a real account before you use this for real."
    echo "  -> LRC Studio's Settings dialog needs: URL http://127.0.0.1:5030"
    echo "     and API key: ${API_KEY}"
else
    echo "$CONFIG already exists, leaving it untouched."
fi

echo
echo "Setup complete. Start it with: ./scripts/run_slskd.sh"

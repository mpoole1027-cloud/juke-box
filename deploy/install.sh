#!/usr/bin/env bash
# Install the party services as launchd agents for the current user, so they
# start at login and restart if they crash.
#
#   deploy/install.sh                 jukebox + Cloudflare tunnel
#   deploy/install.sh --no-tunnel     jukebox only (guests on your Wi-Fi)
#   deploy/install.sh --with-lights   also run party-lights
#
# Undo with deploy/uninstall.sh. Logs go to logs/ in this repo.
set -euo pipefail

JUKEBOX_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LIGHTS_DIR="${PARTY_LIGHTS_DIR:-$(cd "$JUKEBOX_DIR/.." && pwd)/party-lights}"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
WITH_TUNNEL=1
WITH_LIGHTS=0

for arg in "$@"; do
  case "$arg" in
    --no-tunnel) WITH_TUNNEL=0 ;;
    --with-lights) WITH_LIGHTS=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

fail() { echo "✗ $*" >&2; exit 1; }

[ -x "$JUKEBOX_DIR/.venv/bin/python" ] ||
  fail "No virtualenv. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
[ -f "$JUKEBOX_DIR/.env" ] || fail "No .env. Copy .env.example to .env and fill it in."

CLOUDFLARED=""
if [ "$WITH_TUNNEL" = 1 ]; then
  CLOUDFLARED="$(command -v cloudflared || true)"
  [ -n "$CLOUDFLARED" ] || fail "cloudflared not found. Install it: brew install cloudflared"
  [ -f "$JUKEBOX_DIR/deploy/cloudflared/config.yml" ] ||
    fail "No deploy/cloudflared/config.yml. Copy config.yml.example and fill it in (see deploy/README.md)."
fi

if [ "$WITH_LIGHTS" = 1 ]; then
  [ -x "$LIGHTS_DIR/.venv/bin/python" ] ||
    fail "party-lights not found at $LIGHTS_DIR (set PARTY_LIGHTS_DIR)."
fi

mkdir -p "$AGENTS" "$JUKEBOX_DIR/logs"

install_agent() {
  local name="$1"
  local src="$JUKEBOX_DIR/deploy/launchd/$name.plist"
  local dst="$AGENTS/$name.plist"
  sed -e "s|__JUKEBOX_DIR__|$JUKEBOX_DIR|g" \
      -e "s|__LIGHTS_DIR__|$LIGHTS_DIR|g" \
      -e "s|__CLOUDFLARED__|$CLOUDFLARED|g" \
      -e "s|__BEHIND_PROXY__|$WITH_TUNNEL|g" \
      "$src" > "$dst"
  plutil -lint "$dst" >/dev/null
  # Reload cleanly if it was already installed.
  launchctl bootout "$DOMAIN/$name" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$dst"
  echo "✓ $name"
}

install_agent com.partyjukebox.app
[ "$WITH_TUNNEL" = 1 ] && install_agent com.partyjukebox.tunnel
[ "$WITH_LIGHTS" = 1 ] && install_agent com.partyjukebox.lights

echo
echo "Running. Check everything with: .venv/bin/python deploy/preflight.py"
echo "Logs: $JUKEBOX_DIR/logs/"

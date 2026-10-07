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
  if grep -qE '<TUNNEL-UUID>|<you>|party\.example\.com' "$JUKEBOX_DIR/deploy/cloudflared/config.yml"; then
    fail "deploy/cloudflared/config.yml still has placeholders. Fill in the tunnel UUID, credentials path and hostname (cloudflared tunnel list shows the UUID)."
  fi
fi

if [ "$WITH_LIGHTS" = 1 ]; then
  [ -x "$LIGHTS_DIR/.venv/bin/python" ] ||
    fail "party-lights not found at $LIGHTS_DIR (set PARTY_LIGHTS_DIR)."
fi

mkdir -p "$AGENTS" "$JUKEBOX_DIR/logs"

# A jukebox started by hand would hold the port, and two queue managers would
# fight over Spotify. Refuse unless the port belongs to our own launchd job.
PORT="$(grep -E '^PORT=' "$JUKEBOX_DIR/.env" | tail -1 | cut -d= -f2 | tr -d '[:space:]"')"
PORT="${PORT:-5001}"
OWN_PID="$(launchctl print "$DOMAIN/com.partyjukebox.app" 2>/dev/null | awk '/^\tpid =/{print $3}' || true)"
for pid in $(lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null); do
  # The launchd job runs python under caffeinate, so compare parent PIDs too.
  ppid="$(ps -o ppid= -p "$pid" | tr -d ' ')"
  if [ "$pid" != "$OWN_PID" ] && [ "$ppid" != "$OWN_PID" ]; then
    fail "Port $PORT is already in use by PID $pid ($(ps -o command= -p "$pid" | cut -c1-60)). If that's a jukebox you started by hand, stop it (Ctrl+C in its terminal) and re-run."
  fi
done

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
  # Reload cleanly if it was already installed. bootout returns before launchd
  # has finished removing the job, and bootstrapping the same label too soon
  # fails with "Bootstrap failed: 5: Input/output error", so wait for it.
  if launchctl print "$DOMAIN/$name" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$name" 2>/dev/null || true
    for _ in $(seq 1 50); do
      launchctl print "$DOMAIN/$name" >/dev/null 2>&1 || break
      sleep 0.2
    done
  fi
  launchctl bootstrap "$DOMAIN" "$dst" ||
    fail "Couldn't start $name. Re-run deploy/install.sh; if it fails again, see logs/."
  echo "✓ $name"
}

install_agent com.partyjukebox.app
[ "$WITH_TUNNEL" = 1 ] && install_agent com.partyjukebox.tunnel
[ "$WITH_LIGHTS" = 1 ] && install_agent com.partyjukebox.lights
install_agent com.partyjukebox.watchdog

echo
echo "Running. Check everything with: .venv/bin/python deploy/preflight.py"
echo "Logs: $JUKEBOX_DIR/logs/"
grep -q '^PARTY_ALERTS_WEBHOOK=.' "$JUKEBOX_DIR/.env" ||
  echo "Alerts only go to logs/watchdog.log until PARTY_ALERTS_WEBHOOK is set in .env (see deploy/README.md)."

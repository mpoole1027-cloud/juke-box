#!/usr/bin/env bash
# Stop and remove the launchd agents that deploy/install.sh added.
set -uo pipefail
DOMAIN="gui/$(id -u)"
for name in com.partyjukebox.app com.partyjukebox.tunnel com.partyjukebox.lights com.partyjukebox.watchdog; do
  if launchctl bootout "$DOMAIN/$name" 2>/dev/null; then
    echo "✓ stopped $name"
  fi
  rm -f "$HOME/Library/LaunchAgents/$name.plist"
done

#!/bin/sh
# Install two LaunchAgents for the current user:
#   com.anchor.txtav.supervise  starts the run at login (after a reboot)
#   com.anchor.txtav.backup     backs the database up every day at 03:00
# Deliberately no KeepAlive: a supervisor stops for a reason (a halt, an
# expired session) and restarting it in a loop would hide that.
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$REPO/logs"
chmod +x "$REPO/scripts/macos/"*.sh

cat > "$AGENTS/com.anchor.txtav.supervise.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.anchor.txtav.supervise</string>
  <key>ProgramArguments</key><array><string>$REPO/scripts/macos/run-supervisor.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$REPO/logs/launchd-supervise.err</string>
</dict></plist>
PLIST

cat > "$AGENTS/com.anchor.txtav.backup.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.anchor.txtav.backup</string>
  <key>ProgramArguments</key><array><string>$REPO/scripts/macos/backup-db.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$REPO/logs/backup.log</string>
  <key>StandardErrorPath</key><string>$REPO/logs/backup.log</string>
</dict></plist>
PLIST

for name in supervise backup; do
  launchctl bootout "gui/$(id -u)/com.anchor.txtav.$name" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.anchor.txtav.$name.plist"
done
echo "installed; a supervisor already running keeps running (the new one exits on the lock)"

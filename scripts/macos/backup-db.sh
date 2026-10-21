#!/bin/sh
# Copy the job store to a backup folder and keep the newest 14 copies. Use a
# plain folder that Google Drive for desktop syncs, not a path under
# ~/Library/CloudStorage, which macOS will not let a launchd job write to. Uses SQLite's own backup, which is
# safe while the run is writing. Settings and logins are never copied: the
# database holds results and progress only.
set -eu
cd "$(dirname "$0")/../.."
DEST="${TXTAV_BACKUP_DIR:-$(sed -n 's/^TXTAV_BACKUP_DIR=//p' .env 2>/dev/null | tail -1)}"
if [ -z "$DEST" ]; then echo "TXTAV_BACKUP_DIR is not set in .env" >&2; exit 1; fi
mkdir -p "$DEST"
STAMP=$(date -u +%Y-%m-%d)
sqlite3 data/txtav.db ".backup '$DEST/txtav-$STAMP.db'"
ls -1t "$DEST"/txtav-*.db 2>/dev/null | tail -n +15 | while read -r old; do rm -f "$old"; done
echo "backed up to $DEST/txtav-$STAMP.db"

#!/bin/sh
# Start the supervisor from the project folder. Used at login by the
# LaunchAgent, so a reboot does not leave the run stopped. Exits at once if a
# supervisor is already running (the instance lock) or a halt is open.
cd "$(dirname "$0")/../.." || exit 1
mkdir -p logs
exec /usr/bin/caffeinate -dimsu .venv/bin/python -m txtav.cli supervise >> logs/supervise.out 2>&1

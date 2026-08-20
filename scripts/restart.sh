#!/bin/bash

APP_DIR="$HOME/apps/smart-teeth-api"
LOG_FILE="$APP_DIR/server.log"
BIND="127.0.0.1:9411"
cd "$APP_DIR"

echo "Stopping gunicorn ($BIND)..."
if pkill -f "gunicorn --bind $BIND"; then
    # Give the master time to shut its workers down and release the port.
    for _ in $(seq 1 10); do
        pgrep -f "gunicorn --bind $BIND" > /dev/null || break
        sleep 1
    done
    if pgrep -f "gunicorn --bind $BIND" > /dev/null; then
        echo "Still running after 10s — forcing..."
        pkill -9 -f "gunicorn --bind $BIND"
        sleep 1
    fi
else
    echo "No running gunicorn found."
fi

echo "Starting app..."
nohup "$APP_DIR/scripts/run.sh" >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
sleep 2

if pgrep -f "gunicorn --bind $BIND" > /dev/null; then
    echo "Restarted (pid $NEW_PID). Logs: $LOG_FILE"
else
    echo "ERROR: gunicorn did not come up — check $LOG_FILE"
    exit 1
fi

echo "Done at $(date)"

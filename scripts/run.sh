#!/bin/bash

APP_DIR="$HOME/apps/smart-teeth-api"
LOG_DIR="$APP_DIR/logs"
PID_FILE="$APP_DIR/app.pid"

mkdir -p "$LOG_DIR"
cd "$APP_DIR"
source "$APP_DIR/venv/bin/activate"

case "$1" in
  start)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "App is already running (PID: $(cat "$PID_FILE"))"
      exit 1
    fi
    echo "Starting Smart Teeth API..."
    nohup python main.py >> "$LOG_DIR/app.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Started (PID: $!)"
    ;;
  stop)
    if [ -f "$PID_FILE" ]; then
      PID=$(cat "$PID_FILE")
      echo "Stopping (PID: $PID)..."
      kill "$PID" 2>/dev/null
      rm -f "$PID_FILE"
      echo "Stopped"
    else
      echo "Not running"
    fi
    ;;
  restart)
    $0 stop
    sleep 2
    $0 start
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Running (PID: $(cat "$PID_FILE"))"
    else
      echo "Not running"
      rm -f "$PID_FILE"
    fi
    ;;
  logs)
    tail -f "$LOG_DIR/app.log"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs}"
    exit 1
    ;;
esac
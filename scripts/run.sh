#!/bin/bash

APP_DIR="$HOME/apps/smart-teeth-api"
cd "$APP_DIR"
source "$APP_DIR/venv/bin/activate"
exec gunicorn --bind 127.0.0.1:9411 --workers 4 "main:app"
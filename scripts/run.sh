#!/bin/bash

APP_DIR="$HOME/apps/smart-teeth-api"
cd "$APP_DIR"
source "$APP_DIR/venv/bin/activate"
exec python main.py
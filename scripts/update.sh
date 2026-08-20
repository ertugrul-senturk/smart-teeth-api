#!/bin/bash

APP_DIR="$HOME/apps/smart-teeth-api"
cd "$APP_DIR"

echo "Pulling latest code..."
git pull

echo "Installing dependencies..."
source venv/bin/activate
pip install .

echo "Restarting app..."
"$APP_DIR/scripts/restart.sh"

echo "Done at $(date)"
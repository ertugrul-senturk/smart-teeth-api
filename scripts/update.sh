#!/bin/bash

APP_DIR="$HOME/apps/smart-teeth-api"
cd "$APP_DIR"

echo "Pulling latest code..."
git pull

echo "Installing dependencies..."
source venv/bin/activate
pip install .

echo "Restarting app..."
./run.sh restart

echo "Done at $(date)"
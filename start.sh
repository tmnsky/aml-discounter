#!/bin/bash
# Startup script for Render deployment.
# Refreshes sanctions data if stale (>24h), then starts the web server.

set -e

DATA_DIR="${DATA_DIR:-/data}"
PORT="${PORT:-8080}"
INDEX_DB="$DATA_DIR/sanctions_index.db"

echo "=== AML Discounter Startup ==="

# Check if data needs refresh (missing or older than 24h)
NEEDS_REFRESH=false
if [ ! -f "$INDEX_DB" ]; then
    echo "No sanctions index found. Running initial data fetch..."
    NEEDS_REFRESH=true
elif [ "$(find "$INDEX_DB" -mmin +1440 2>/dev/null)" ]; then
    echo "Sanctions index is older than 24 hours. Refreshing..."
    NEEDS_REFRESH=true
else
    echo "Sanctions index is fresh. Skipping refresh."
fi

if [ "$NEEDS_REFRESH" = true ]; then
    echo "Fetching sanctions data (this takes 5-10 minutes on first run)..."
    python -m app.cli refresh || echo "WARNING: Data refresh failed. Starting with existing data."
    echo "Data refresh complete."
fi

echo "Starting server on port $PORT..."
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"

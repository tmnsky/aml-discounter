#!/bin/bash
# Startup script for Render deployment.
# Starts uvicorn immediately, then refreshes sanctions data in the background
# if stale (>24h) or missing.

set -e

DATA_DIR="${DATA_DIR:-/data}"
PORT="${PORT:-8080}"
INDEX_DB="$DATA_DIR/sanctions_index.db"

echo "=== AML Discounter Startup ==="

# Check if data needs refresh (missing or older than 24h)
NEEDS_REFRESH=false
if [ ! -f "$INDEX_DB" ]; then
    echo "No sanctions index found. Will fetch in background after server starts."
    NEEDS_REFRESH=true
elif [ "$(find "$INDEX_DB" -mmin +1440 2>/dev/null)" ]; then
    echo "Sanctions index is older than 24 hours. Will refresh in background."
    NEEDS_REFRESH=true
else
    echo "Sanctions index is fresh. Skipping refresh."
fi

# Start data refresh in background if needed
if [ "$NEEDS_REFRESH" = true ]; then
    (
        echo "Background: fetching sanctions data..."
        python -m app.cli refresh && echo "Background: data refresh complete." || echo "WARNING: Background data refresh failed."
    ) &
fi

echo "Starting server on port $PORT..."
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"

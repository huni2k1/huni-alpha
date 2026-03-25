#!/bin/bash
# Quick start script for market-scanner-v2

set -e

WORKSPACE_DIR="/Users/ninhvan/.openclaw/workspace"
cd "$WORKSPACE_DIR"

echo "🚀 Starting Market Scanner v2..."

# Check if Python script exists
if [ ! -f "scripts/market-scanner-v2.py" ]; then
    echo "❌ Error: scripts/market-scanner-v2.py not found"
    exit 1
fi

# Run the scanner directly (no onboard needed)
python3 scripts/market-scanner-v2.py

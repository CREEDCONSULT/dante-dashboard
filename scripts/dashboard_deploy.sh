#!/bin/bash
# Dante Dashboard Deploy — curl live server HTML → GitHub Pages
set -e

SERVER="http://localhost:8765"
DEPLOY_DIR="/tmp/dante-dashboard"
OUTPUT="$DEPLOY_DIR/index.html"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

# Ensure server is running
if ! curl -sf "$SERVER/api/ping" > /dev/null 2>&1; then
    echo "Server not running, skipping deploy"
    exit 0
fi

# Fetch rendered dashboard
curl -sf "$SERVER/" -o "/tmp/dante-dash-tmp.html" || { echo "Failed to fetch dashboard"; exit 1; }

# Deploy to GitHub Pages
cd "$DEPLOY_DIR"
git remote update 2>/dev/null || true
git reset --hard origin/gh-pages 2>/dev/null || true
cp /tmp/dante-dash-tmp.html index.html
git add index.html
git commit -m "Update $TIMESTAMP" 2>/dev/null || true
git push origin gh-pages 2>/dev/null || { echo "Push failed (network?)"; exit 0; }

echo "✅ Deployed to GitHub Pages at $TIMESTAMP"
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/huni-alpha"

cd "$APP_DIR"

git fetch origin
git reset --hard origin/main

source .venv/bin/activate
pip install -r requirements.txt

mkdir -p data

sudo systemctl restart huni-alpha
sudo systemctl status huni-alpha --no-pager

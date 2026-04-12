#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/huni-alpha"
SERVICE_NAME="huni-alpha"
SERVICE_FILE_SRC="$APP_DIR/deploy/huni-alpha.service"
SERVICE_FILE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
RUNTIME_ENV_FILE="$APP_DIR/deploy/runtime.env"

cd "$APP_DIR"

git fetch origin
git reset --hard origin/main

APP_VERSION="${APP_VERSION:-$(git rev-parse --short HEAD)}"
printf "APP_VERSION=%s\n" "$APP_VERSION" > "$RUNTIME_ENV_FILE"

cp "$SERVICE_FILE_SRC" "$SERVICE_FILE_DST"
systemctl daemon-reload

source .venv/bin/activate
pip install -r requirements.txt

mkdir -p data

systemctl restart "$SERVICE_NAME"
systemctl status "$SERVICE_NAME" --no-pager

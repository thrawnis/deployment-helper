#!/usr/bin/env bash
set -euo pipefail

echo "==> Switching to dev branch and pulling latest..."
git checkout dev
git pull origin dev

echo "==> Stopping existing container..."
docker compose down

echo "==> Building image..."
docker compose build --no-cache

echo "==> Starting container..."
docker compose up -d

echo "==> Done. Dashboard running on port 8000."

#!/usr/bin/env bash
set -euo pipefail

echo "==> Switching to dev branch and pulling latest..."
git checkout dev
git pull origin dev

echo "==> Building new image (container stays up during build)..."
docker compose build --no-cache

echo "==> Handing restart off to Docker host..."
# Spawn a lightweight helper container on the host that outlives this container.
# It waits 3 seconds for this script to finish logging, then swaps the container.
docker run --rm -d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd):$(pwd)" \
  -w "$(pwd)" \
  docker:cli \
  sh -c "sleep 3 && docker compose down && docker compose up -d"

echo "==> Restart scheduled. Container going down now..."

#!/usr/bin/env bash
# rebuild.sh — Pull latest dev branch and redeploy the Deployment Dashboard.
# Usage: ./rebuild.sh [--pull-base-images]
#
# Builds the image directly with `docker buildx build` rather than
# `docker compose build` — Compose's buildx plugin detection is unreliable
# and can silently fall back to the legacy builder, which fails on any
# BuildKit-only Dockerfile syntax. `docker buildx build` goes through the
# CLI's own plugin resolution, which is consistent.
#
# Pass --pull-base-images occasionally (e.g. monthly) to re-check the base
# image against the registry. Omitted by default — it's pure network latency
# on every rebuild for essentially zero benefit day-to-day.

set -euo pipefail

PULL_BASE_IMAGES=false
for arg in "$@"; do
  [ "$arg" = "--pull-base-images" ] && PULL_BASE_IMAGES=true
done

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE="deployment-helper-dashboard:latest"

echo "==> Switching to dev branch and pulling latest..."
cd "$REPO_DIR"
git checkout dev
git pull origin dev

echo "==> Building image with docker buildx"
BUILDX_ARGS=(build --tag "$IMAGE" --load "$REPO_DIR")
if [ "$PULL_BASE_IMAGES" = true ]; then
  echo "    (--pull-base-images: re-checking base image against registry)"
  BUILDX_ARGS+=(--pull)
fi
docker buildx "${BUILDX_ARGS[@]}"

echo "==> Handing restart off to Docker host..."
# Spawn a lightweight helper container on the host that outlives this one.
# It waits 3 seconds for this script to finish logging, then swaps the container.
docker run --rm -d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$REPO_DIR:$REPO_DIR" \
  -w "$REPO_DIR" \
  docker:cli \
  sh -c "sleep 3 && docker compose down && docker compose up -d --no-build"

echo "==> Restart scheduled. Container going down now..."

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
# Spawn a helper container that outlives this one to do the actual swap.
# Use the image we just built (not a separate docker:cli pull) — it's
# already verified to have docker-ce-cli + compose + buildx installed,
# whereas docker:cli's bundled compose plugin has been unreliable and
# fails silently since this container runs detached with no visible output.
# Output is captured to data/restart.log so a failure here is diagnosable.
RESTART_LOG="$REPO_DIR/data/restart.log"
mkdir -p "$REPO_DIR/data"
docker run --rm -d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$REPO_DIR:$REPO_DIR" \
  -w "$REPO_DIR" \
  --entrypoint sh \
  "$IMAGE" \
  -c "sleep 3 && { docker compose down && docker compose up -d --no-build; } > '$RESTART_LOG' 2>&1"

echo "==> Restart scheduled. Container going down now..."

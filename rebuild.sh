#!/usr/bin/env bash
# rebuild.sh — Pull latest main branch and redeploy the Deployment Dashboard,
# but only if there's actually something new to deploy.
# Usage: ./rebuild.sh [--pull-base-images] [--force]
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
#
# Pass --force to rebuild and restart even if the git pull brought in no new
# commits — useful after changing something outside git (e.g. .env).
#
# Even with no new commits, this also force-rebuilds automatically if the
# currently *running* container isn't actually on the latest commit — e.g.
# a previous restart silently failed and left stale code running. This is
# detected via a git_sha label baked into the image at build time.

set -euo pipefail

PULL_BASE_IMAGES=false
FORCE=false
for arg in "$@"; do
  case "$arg" in
    --pull-base-images) PULL_BASE_IMAGES=true ;;
    --force) FORCE=true ;;
  esac
done

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE="deployment-helper-dashboard:latest"

cd "$REPO_DIR"

echo "==> Discarding local changes and pulling latest from main..."
git fetch origin main
git checkout main
BEFORE_SHA="$(git rev-parse HEAD)"
git reset --hard origin/main
git clean -fd
AFTER_SHA="$(git rev-parse HEAD)"

# What commit is the currently running container actually built from?
RUNNING_SHA=""
RUNNING_CID="$(docker compose ps -q dashboard 2>/dev/null || true)"
if [ -n "$RUNNING_CID" ]; then
  RUNNING_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$RUNNING_CID" 2>/dev/null || true)"
  if [ -n "$RUNNING_IMAGE_ID" ]; then
    RUNNING_SHA="$(docker image inspect -f '{{ index .Config.Labels "git_sha" }}' "$RUNNING_IMAGE_ID" 2>/dev/null || true)"
  fi
fi

if [ "$BEFORE_SHA" = "$AFTER_SHA" ] && [ "$FORCE" = false ]; then
  if [ -z "$RUNNING_SHA" ] || [ "$RUNNING_SHA" = "$AFTER_SHA" ]; then
    echo "==> No new commits (still at ${AFTER_SHA:0:7}). Nothing to do — skipping build and restart."
    echo "    Pass --force to rebuild and restart anyway."
    exit 0
  fi
  echo "==> No new commits, but the running container is on ${RUNNING_SHA:0:7} instead of ${AFTER_SHA:0:7}."
  echo "    Forcing a rebuild to bring it in sync."
fi

if [ "$BEFORE_SHA" = "$AFTER_SHA" ]; then
  echo "==> No new commits, but --force was given — rebuilding anyway."
else
  echo "==> Code changed (${BEFORE_SHA:0:7} -> ${AFTER_SHA:0:7})."
fi

echo "==> Building image with docker buildx"
BUILDX_ARGS=(build --tag "$IMAGE" --build-arg "GIT_SHA=$AFTER_SHA" --load "$REPO_DIR")
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

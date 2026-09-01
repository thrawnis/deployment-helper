#!/usr/bin/env python3
"""
Scheduled deploy script — triggers all project deploys sequentially via the
dashboard API.  Intended to run as a host-level cron job:

    0 3 * * * /usr/bin/python3 /media/docker/deployment-helper/scheduled-deploy.py

Deployment Helper is deployed first (if configured).  After it restarts the
container, the script waits for the dashboard to come back before continuing
with all remaining projects.

Logs are written to ./data/scheduled-deploy.log alongside deploy history.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────

DASHBOARD_URL   = "http://localhost:8089"
DASHBOARD_NAME  = "deployment helper"
LOG_FILE        = Path(__file__).parent / "data" / "scheduled-deploy.log"
POLL_INTERVAL   = 10   # seconds between deploy status checks
DEPLOY_TIMEOUT  = 600  # max seconds to wait for a single deploy (10 min)
RESTART_RETRIES = 24   # how many times to probe after a Deployment Helper redeploy
RESTART_WAIT    = 5    # seconds between restart probes

# ── Logging ────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────


def _get(path: str):
    with urllib.request.urlopen(f"{DASHBOARD_URL}{path}", timeout=10) as r:
        return json.loads(r.read())


def _post(path: str):
    req = urllib.request.Request(
        f"{DASHBOARD_URL}{path}", method="POST", data=b""
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def wait_for_dashboard(retries: int = 12, interval: int = 5) -> bool:
    for _ in range(retries):
        try:
            _get("/api/projects")
            return True
        except Exception:
            time.sleep(interval)
    return False


def deploy_and_wait(project: dict) -> bool:
    pid, name = project["id"], project["name"]
    log.info(f"Starting deploy: {name}")

    try:
        _post(f"/api/projects/{pid}/deploy")
    except urllib.error.HTTPError as exc:
        log.error(f"Could not start deploy for {name}: HTTP {exc.code}")
        return False
    except Exception as exc:
        log.error(f"Could not start deploy for {name}: {exc}")
        return False

    elapsed = 0
    while elapsed < DEPLOY_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        try:
            projects = _get("/api/projects")
        except Exception:
            continue
        p = next((p for p in projects if p["id"] == pid), None)
        if not p or p.get("is_deploying"):
            continue
        success = bool((p.get("last_deploy") or {}).get("success"))
        if success:
            log.info(f"✓ {name} — succeeded")
        else:
            log.warning(f"✗ {name} — failed")
        return success

    log.error(f"Deploy timed out for {name}")
    return False


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> None:
    log.info("=" * 50)
    log.info("Scheduled deploy started")

    if not wait_for_dashboard():
        log.error("Dashboard not reachable — aborting")
        return

    projects = _get("/api/projects")
    dashboard = next(
        (p for p in projects if p["name"].strip().lower() == DASHBOARD_NAME), None
    )
    others = [
        p for p in projects if p["name"].strip().lower() != DASHBOARD_NAME
    ]

    # Deploy Deployment Helper first so it gets the latest code before
    # deploying the other projects.
    if dashboard:
        deploy_and_wait(dashboard)
        log.info("Waiting for dashboard container to come back online…")
        if wait_for_dashboard(retries=RESTART_RETRIES, interval=RESTART_WAIT):
            log.info("Dashboard is back online — continuing with remaining projects")
        else:
            log.error("Dashboard did not come back in time — aborting remaining deploys")
            return

    for project in others:
        deploy_and_wait(project)
        time.sleep(5)  # brief pause between deploys

    log.info("Scheduled deploy complete")
    log.info("=" * 50)


if __name__ == "__main__":
    main()

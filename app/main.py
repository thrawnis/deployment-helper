"""Deployment Dashboard — FastAPI backend."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = DATA_DIR / "history.json"
MAX_HISTORY = 5
DEPLOY_STATE_TTL = 120  # seconds to keep finished deploy state in memory


def _load_projects() -> list[dict]:
    projects: list[dict] = []
    i = 1
    while name := os.getenv(f"PROJECT_{i}_NAME"):
        projects.append(
            {
                "id": str(i),
                "name": name,
                "path": os.getenv(f"PROJECT_{i}_PATH", ""),
                "script": os.getenv(f"PROJECT_{i}_SCRIPT", "./rebuild.sh"),
            }
        )
        i += 1
    return projects


DASHBOARD_NAME = "deployment helper"

PROJECTS: list[dict] = sorted(
    _load_projects(),
    key=lambda p: (p["name"].strip().lower() == DASHBOARD_NAME, p["name"].strip().lower()),
)
PROJECT_MAP: dict[str, dict] = {p["id"]: p for p in PROJECTS}

# ── App ────────────────────────────────────────────────────────────────────

app = FastAPI(title="Deployment Dashboard")


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Persistence ────────────────────────────────────────────────────────────


def _load_history() -> dict:
    try:
        return json.loads(HISTORY_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_history(h: dict) -> None:
    HISTORY_FILE.write_text(json.dumps(h, indent=2))


# ── Active deploy state ────────────────────────────────────────────────────


class DeployState:
    def __init__(self, deploy_id: str) -> None:
        self.id = deploy_id
        self.started_at: str = datetime.now(timezone.utc).isoformat()
        self.logs: list[dict] = []
        self.done: bool = False
        self.success: bool | None = None
        self.finished_at: str | None = None


# project_id -> DeployState (kept for DEPLOY_STATE_TTL seconds after completion)
_active: dict[str, DeployState] = {}

# ── Git helpers ────────────────────────────────────────────────────────────


def _git(path: str, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", path, *args],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        return ""


def get_git_info(path: str) -> dict:
    branch = _git(path, "branch", "--show-current") or _git(
        path, "rev-parse", "--abbrev-ref", "HEAD"
    )
    log_line = _git(path, "log", "-1", "--format=%s|||%ai")
    msg, _, ts = log_line.partition("|||")
    return {
        "branch": branch or "unknown",
        "last_commit": msg.strip(),
        "last_commit_time": ts.strip(),
    }


# ── Routes ─────────────────────────────────────────────────────────────────


def _any_other_deploy_active(project_id: str) -> bool:
    return any(
        pid != project_id and not s.done
        for pid, s in _active.items()
    )


def _is_dashboard(project: dict) -> bool:
    return project["name"].strip().lower() == DASHBOARD_NAME


@app.get("/api/projects")
async def list_projects() -> list[dict]:
    history = _load_history()
    result = []
    for p in PROJECTS:
        git = get_git_info(p["path"])
        proj_history = history.get(p["id"], [])
        last_deploy = proj_history[0] if proj_history else None
        state = _active.get(p["id"])
        is_deploying = bool(state and not state.done)
        deploy_blocked = (
            _is_dashboard(p) and _any_other_deploy_active(p["id"])
        )
        result.append(
            {
                **p,
                "git": git,
                "last_deploy": last_deploy,
                "is_deploying": is_deploying,
                "deploy_blocked": deploy_blocked,
            }
        )
    return result


@app.post("/api/projects/{project_id}/deploy")
async def start_deploy(project_id: str) -> dict:
    project = PROJECT_MAP.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    existing = _active.get(project_id)
    if existing and not existing.done:
        raise HTTPException(409, "Deploy already in progress")

    if _is_dashboard(project) and _any_other_deploy_active(project_id):
        raise HTTPException(409, "Cannot redeploy dashboard while another deploy is in progress")

    state = DeployState(str(uuid.uuid4())[:8])
    _active[project_id] = state
    asyncio.create_task(_run_deploy(project_id, project, state))
    return {"deploy_id": state.id}


@app.get("/api/projects/{project_id}/stream")
async def stream_deploy(project_id: str) -> StreamingResponse:
    state = _active.get(project_id)
    if not state:
        raise HTTPException(404, "No recent deploy for this project")

    async def generate():
        yield f"data: {json.dumps({'type': 'connected', 'deploy_id': state.id})}\n\n"
        idx = 0
        while True:
            while idx < len(state.logs):
                yield f"data: {json.dumps(state.logs[idx])}\n\n"
                idx += 1
            if state.done:
                yield (
                    f"data: {json.dumps({'type': 'end', 'success': state.success})}\n\n"
                )
                break
            await asyncio.sleep(0.05)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/projects/{project_id}/history")
async def get_history(project_id: str) -> list:
    if project_id not in PROJECT_MAP:
        raise HTTPException(404, "Project not found")
    return _load_history().get(project_id, [])


# ── Deploy runner ──────────────────────────────────────────────────────────


async def _run_deploy(project_id: str, project: dict, state: DeployState) -> None:
    try:
        proc = await asyncio.create_subprocess_shell(
            project["script"],
            cwd=project["path"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "TERM": "xterm-256color", "FORCE_COLOR": "1"},
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            state.logs.append(
                {"type": "log", "text": raw.decode(errors="replace").rstrip()}
            )
        await proc.wait()
        state.success = proc.returncode == 0
    except Exception as exc:
        state.logs.append({"type": "log", "text": f"ERROR: {exc}"})
        state.success = False

    state.finished_at = datetime.now(timezone.utc).isoformat()
    state.done = True

    history = _load_history()
    history.setdefault(project_id, []).insert(
        0,
        {
            "id": state.id,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "success": state.success,
            "output": [e["text"] for e in state.logs if e.get("type") == "log"],
        },
    )
    history[project_id] = history[project_id][:MAX_HISTORY]
    _save_history(history)

    # Keep state accessible briefly so late-connecting SSE clients can read logs
    asyncio.create_task(_expire_state(project_id, DEPLOY_STATE_TTL))


async def _expire_state(project_id: str, delay: int) -> None:
    await asyncio.sleep(delay)
    state = _active.get(project_id)
    if state and state.done:
        del _active[project_id]

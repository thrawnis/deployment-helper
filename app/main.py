"""Deployment Dashboard — FastAPI backend."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = DATA_DIR / "history.json"
CONFIG_FILE = DATA_DIR / "projects.json"
MAX_HISTORY = 5
DEPLOY_STATE_TTL = 120  # seconds to keep finished deploy state in memory
DASHBOARD_NAME = "deployment helper"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_COOKIE = "admin_session"
SESSION_TTL = 8 * 3600  # 8 hours

# ── Project config (stored in DATA_DIR, editable via the admin UI) ─────────


def _seed_config_from_env() -> list[dict]:
    """One-time migration: read legacy PROJECT_N_* vars from .env."""
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


def _load_config() -> list[dict]:
    if not CONFIG_FILE.exists():
        seeded = _seed_config_from_env()
        _save_config(seeded)
        return seeded
    try:
        return json.loads(CONFIG_FILE.read_text())
    except json.JSONDecodeError:
        return []


def _save_config(projects: list[dict]) -> None:
    CONFIG_FILE.write_text(json.dumps(projects, indent=2))


def get_projects() -> list[dict]:
    return sorted(
        _load_config(),
        key=lambda p: (
            p["name"].strip().lower() == DASHBOARD_NAME,
            p["name"].strip().lower(),
        ),
    )


def get_project_map() -> dict[str, dict]:
    return {p["id"]: p for p in get_projects()}


# ── App ────────────────────────────────────────────────────────────────────

app = FastAPI(title="Deployment Dashboard")


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Deploy history persistence ──────────────────────────────────────────────


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

# ── Admin auth ───────────────────────────────────────────────────────────────

_sessions: dict[str, float] = {}  # token -> expiry epoch seconds


def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def _valid_session(token: str | None) -> bool:
    if not token:
        return False
    expiry = _sessions.get(token)
    if not expiry or expiry < time.time():
        _sessions.pop(token, None)
        return False
    return True


async def require_admin(request: Request) -> None:
    if not _valid_session(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(401, "Admin authentication required")


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


def _dashboard_deploy_active() -> bool:
    project_map = get_project_map()
    return any(
        pid in project_map and _is_dashboard(project_map[pid]) and not s.done
        for pid, s in _active.items()
    )


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
    for p in get_projects():
        git = get_git_info(p["path"])
        proj_history = history.get(p["id"], [])
        last_deploy = proj_history[0] if proj_history else None
        state = _active.get(p["id"])
        is_deploying = bool(state and not state.done)
        deploy_blocked = (
            (_is_dashboard(p) and _any_other_deploy_active(p["id"]))
            or (not _is_dashboard(p) and _dashboard_deploy_active())
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
    project = get_project_map().get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    existing = _active.get(project_id)
    if existing and not existing.done:
        raise HTTPException(409, "Deploy already in progress")

    if _is_dashboard(project) and _any_other_deploy_active(project_id):
        raise HTTPException(409, "Cannot redeploy dashboard while another deploy is in progress")

    if not _is_dashboard(project) and _dashboard_deploy_active():
        raise HTTPException(409, "Cannot deploy while Deployment Helper is redeploying")

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
    if project_id not in get_project_map():
        raise HTTPException(404, "Project not found")
    return _load_history().get(project_id, [])


@app.get("/api/projects/{project_id}/docker-logs")
async def docker_logs(project_id: str) -> dict:
    project = get_project_map().get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    try:
        result = subprocess.run(
            ["docker", "compose", "logs", "api", "--tail=50", "--no-color"],
            cwd=project["path"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = result.stdout or result.stderr or "(no output)"
    except Exception as exc:
        output = f"ERROR: {exc}"
    return {"output": output}


# ── Admin routes (project config management) ────────────────────────────────


@app.post("/api/admin/login")
async def admin_login(payload: dict, response: Response) -> dict:
    if not ADMIN_PASSWORD:
        raise HTTPException(500, "ADMIN_PASSWORD is not configured on the server")
    password = str(payload.get("password", ""))
    if not secrets.compare_digest(password, ADMIN_PASSWORD):
        raise HTTPException(401, "Incorrect password")
    token = _new_session()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL,
    )
    return {"ok": True}


@app.post("/api/admin/logout")
async def admin_logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        _sessions.pop(token, None)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/admin/session")
async def admin_session(request: Request) -> dict:
    return {"authenticated": _valid_session(request.cookies.get(SESSION_COOKIE))}


@app.get("/api/admin/projects", dependencies=[Depends(require_admin)])
async def admin_list_projects() -> list[dict]:
    return get_projects()


@app.post("/api/admin/projects", dependencies=[Depends(require_admin)])
async def admin_create_project(payload: dict) -> dict:
    name = str(payload.get("name", "")).strip()
    path = str(payload.get("path", "")).strip()
    script = str(payload.get("script", "./rebuild.sh")).strip() or "./rebuild.sh"
    if not name or not path:
        raise HTTPException(400, "name and path are required")

    projects = _load_config()
    if any(p["name"].strip().lower() == name.lower() for p in projects):
        raise HTTPException(409, "A project with this name already exists")

    project = {"id": secrets.token_hex(4), "name": name, "path": path, "script": script}
    projects.append(project)
    _save_config(projects)
    return project


@app.put("/api/admin/projects/{project_id}", dependencies=[Depends(require_admin)])
async def admin_update_project(project_id: str, payload: dict) -> dict:
    projects = _load_config()
    project = next((p for p in projects if p["id"] == project_id), None)
    if not project:
        raise HTTPException(404, "Project not found")

    name = str(payload.get("name", project["name"])).strip()
    path = str(payload.get("path", project["path"])).strip()
    script = str(payload.get("script", project["script"])).strip() or "./rebuild.sh"
    if not name or not path:
        raise HTTPException(400, "name and path are required")
    if any(
        p["id"] != project_id and p["name"].strip().lower() == name.lower()
        for p in projects
    ):
        raise HTTPException(409, "A project with this name already exists")

    project["name"] = name
    project["path"] = path
    project["script"] = script
    _save_config(projects)
    return project


@app.delete("/api/admin/projects/{project_id}", dependencies=[Depends(require_admin)])
async def admin_delete_project(project_id: str) -> dict:
    projects = _load_config()
    filtered = [p for p in projects if p["id"] != project_id]
    if len(filtered) == len(projects):
        raise HTTPException(404, "Project not found")
    _save_config(filtered)
    return {"ok": True}


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

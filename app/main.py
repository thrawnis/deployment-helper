"""Deployment Dashboard — FastAPI backend."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import smtplib
import subprocess
import time
import uuid
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
SCHEDULE_FILE = DATA_DIR / "schedule.json"
SCHEDULE_RUN_FILE = DATA_DIR / "schedule_run.json"
SCHEDULE_TRIGGER_FILE = DATA_DIR / "schedule_last_trigger.json"
SCHEDULE_LOG_FILE = DATA_DIR / "schedule.log"
MAX_HISTORY = 5
DEPLOY_STATE_TTL = 120  # seconds to keep finished deploy state in memory
DASHBOARD_NAME = "deployment helper"
DEFAULT_SCHEDULE_TIME = "03:00"
DEFAULT_SCHEDULE_TZ = "UTC"
SCHEDULER_POLL_INTERVAL = 30  # seconds

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_COOKIE = "admin_session"
SESSION_TTL = 8 * 3600  # 8 hours

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
SMTP_TO = [addr.strip() for addr in os.getenv("SMTP_TO", "").split(",") if addr.strip()]
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").strip().lower() not in ("false", "0", "no")

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
                "scheduled": False,
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
        projects = json.loads(CONFIG_FILE.read_text())
    except json.JSONDecodeError:
        return []
    for p in projects:
        p.setdefault("scheduled", False)
    return projects


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


# ── Daily schedule persistence ──────────────────────────────────────────────


def _parse_hhmm(value: str) -> tuple[int, int] | tuple[None, None]:
    try:
        hh_s, mm_s = value.strip().split(":")
        hh, mm = int(hh_s), int(mm_s)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except Exception:
        pass
    return None, None


def _parse_tz(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def _load_schedule() -> dict:
    try:
        data = json.loads(SCHEDULE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("time", DEFAULT_SCHEDULE_TIME)
    data.setdefault("timezone", DEFAULT_SCHEDULE_TZ)
    return data


def _save_schedule(data: dict) -> None:
    SCHEDULE_FILE.write_text(json.dumps(data, indent=2))


def _load_schedule_run() -> dict | None:
    try:
        return json.loads(SCHEDULE_RUN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_schedule_run(data: dict) -> None:
    SCHEDULE_RUN_FILE.write_text(json.dumps(data, indent=2))


def _clear_schedule_run() -> None:
    SCHEDULE_RUN_FILE.unlink(missing_ok=True)


def _load_last_trigger_date() -> str | None:
    try:
        return json.loads(SCHEDULE_TRIGGER_FILE.read_text()).get("date")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_last_trigger_date(value: str) -> None:
    SCHEDULE_TRIGGER_FILE.write_text(json.dumps({"date": value}))


def _schedule_log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    with SCHEDULE_LOG_FILE.open("a") as f:
        f.write(line)


# ── Nightly schedule email report ───────────────────────────────────────────


def _build_report_email(results: list[dict]) -> tuple[str, str]:
    today = date.today().isoformat()
    subject = f"Deployment Helper — nightly schedule report ({today})"

    status_labels = {
        "success": "✅ Success",
        "failed": "❌ Failed",
        "skipped": "⏭️ Skipped",
    }
    row_html = []
    for r in results:
        status = status_labels.get(r.get("status"), r.get("status", "unknown"))
        restarted = r.get("container_restarted")
        updated = "Yes" if restarted is True else "No" if restarted is False else "Unknown"
        row_html.append(
            "<tr>"
            f"<td style='padding:6px 12px;border:1px solid #ccc;'>{r.get('name', '')}</td>"
            f"<td style='padding:6px 12px;border:1px solid #ccc;'>{status}</td>"
            f"<td style='padding:6px 12px;border:1px solid #ccc;'>{updated}</td>"
            "</tr>"
        )
    table_body = "\n".join(row_html) or (
        "<tr><td colspan='3' style='padding:6px 12px;'>No projects were scheduled.</td></tr>"
    )

    html = f"""\
<html>
<body style="font-family: -apple-system, Arial, sans-serif; color:#111;">
  <h2 style="margin-bottom:0.25rem;">Nightly deploy schedule report</h2>
  <p style="color:#555; margin-top:0;">{today}</p>
  <table style="border-collapse: collapse; font-size: 14px;">
    <thead>
      <tr>
        <th style="padding:6px 12px;border:1px solid #ccc; text-align:left; background:#f3f3f3;">App</th>
        <th style="padding:6px 12px;border:1px solid #ccc; text-align:left; background:#f3f3f3;">Result</th>
        <th style="padding:6px 12px;border:1px solid #ccc; text-align:left; background:#f3f3f3;">Updated</th>
      </tr>
    </thead>
    <tbody>
      {table_body}
    </tbody>
  </table>
</body>
</html>
"""
    return subject, html


def _send_email_sync(subject: str, html_body: str) -> None:
    if not SMTP_HOST or not SMTP_FROM or not SMTP_TO:
        raise RuntimeError(
            "SMTP is not configured — set SMTP_HOST, SMTP_FROM, and SMTP_TO in .env"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(SMTP_TO)
    msg.attach(MIMEText(html_body, "html"))

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            if SMTP_USERNAME:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, SMTP_TO, msg.as_string())
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USERNAME:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, SMTP_TO, msg.as_string())


async def _send_schedule_report(results: list[dict]) -> None:
    if not results:
        _schedule_log("No projects were scheduled — skipping email report")
        return
    subject, html = _build_report_email(results)
    try:
        await asyncio.to_thread(_send_email_sync, subject, html)
        _schedule_log(f"Email report sent to {', '.join(SMTP_TO)}")
    except Exception as exc:
        _schedule_log(f"Failed to send email report: {exc}")


# ── Active deploy state ────────────────────────────────────────────────────


class DeployState:
    def __init__(self, deploy_id: str) -> None:
        self.id = deploy_id
        self.started_at: str = datetime.now(timezone.utc).isoformat()
        self.logs: list[dict] = []
        self.done: bool = False
        self.success: bool | None = None
        self.finished_at: str | None = None
        self.container_restarted: bool | None = None


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


def _get_container_state(path: str, service: str = "api") -> dict | None:
    """Container id + start time for a project's `service`, or None if not found."""
    try:
        cid = subprocess.check_output(
            ["docker", "compose", "ps", "-q", service],
            cwd=path,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()
        if not cid:
            return None
        started_at = subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.StartedAt}}", cid],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()
        return {"id": cid, "started_at": started_at}
    except Exception:
        return None


def _container_restarted(before: dict | None, after: dict | None) -> bool | None:
    """True/False if we can tell, None if no container was found either time."""
    if before is None and after is None:
        return None
    if before is None or after is None:
        return True
    return before["id"] != after["id"] or before["started_at"] != after["started_at"]


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
                    f"data: {json.dumps({'type': 'end', 'success': state.success, 'container_restarted': state.container_restarted})}\n\n"
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
    scheduled = bool(payload.get("scheduled", False))
    if not name or not path:
        raise HTTPException(400, "name and path are required")

    projects = _load_config()
    if any(p["name"].strip().lower() == name.lower() for p in projects):
        raise HTTPException(409, "A project with this name already exists")

    project = {
        "id": secrets.token_hex(4),
        "name": name,
        "path": path,
        "script": script,
        "scheduled": scheduled,
    }
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
    scheduled = bool(payload.get("scheduled", project.get("scheduled", False)))
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
    project["scheduled"] = scheduled
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


@app.get("/api/admin/schedule", dependencies=[Depends(require_admin)])
async def admin_get_schedule() -> dict:
    return _load_schedule()


@app.put("/api/admin/schedule", dependencies=[Depends(require_admin)])
async def admin_set_schedule(payload: dict) -> dict:
    time_str = str(payload.get("time", "")).strip()
    hh, mm = _parse_hhmm(time_str)
    if hh is None:
        raise HTTPException(400, "time must be in HH:MM 24-hour format")

    tz_name = str(payload.get("timezone", "")).strip() or DEFAULT_SCHEDULE_TZ
    if _parse_tz(tz_name) is None:
        raise HTTPException(
            400, f'Unknown timezone "{tz_name}" — use an IANA name like America/New_York or UTC'
        )

    data = {"time": f"{hh:02d}:{mm:02d}", "timezone": tz_name}
    _save_schedule(data)
    return data


@app.post("/api/admin/schedule/test-email", dependencies=[Depends(require_admin)])
async def admin_test_email() -> dict:
    subject, html = _build_report_email(
        [
            {"name": "Example App", "status": "success", "container_restarted": True},
            {"name": "Other App", "status": "failed", "container_restarted": False},
        ]
    )
    try:
        await asyncio.to_thread(_send_email_sync, f"[Test] {subject}", html)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "sent_to": SMTP_TO}


# ── Deploy runner ──────────────────────────────────────────────────────────


async def _run_deploy(project_id: str, project: dict, state: DeployState) -> None:
    before_container = _get_container_state(project["path"])
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

    after_container = _get_container_state(project["path"])
    state.container_restarted = _container_restarted(before_container, after_container)

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
            "container_restarted": state.container_restarted,
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


# ── Daily scheduler ──────────────────────────────────────────────────────────


def _scheduled_project_ids() -> list[str]:
    """Deployment Helper first (if included), then the rest alphabetically."""
    projects = [p for p in get_projects() if p.get("scheduled")]
    projects.sort(key=lambda p: (not _is_dashboard(p), p["name"].strip().lower()))
    return [p["id"] for p in projects]


async def _scheduled_deploy_one(project: dict) -> dict:
    pid, name = project["id"], project["name"]

    existing = _active.get(pid)
    if existing and not existing.done:
        _schedule_log(f"Skipping {name}: a deploy is already in progress")
        return {"name": name, "status": "skipped", "container_restarted": None}
    if _is_dashboard(project) and _any_other_deploy_active(pid):
        _schedule_log(f"Skipping {name}: another deploy is in progress")
        return {"name": name, "status": "skipped", "container_restarted": None}
    if not _is_dashboard(project) and _dashboard_deploy_active():
        _schedule_log(f"Skipping {name}: Deployment Helper is redeploying")
        return {"name": name, "status": "skipped", "container_restarted": None}

    state = DeployState(str(uuid.uuid4())[:8])
    _active[pid] = state
    _schedule_log(f"Starting: {name}")
    await _run_deploy(pid, project, state)
    _schedule_log(f"Finished: {name} (success={state.success})")
    return {
        "name": name,
        "status": "success" if state.success else "failed",
        "container_restarted": state.container_restarted,
    }


async def _run_scheduled_deploys(
    project_ids: list[str], prior_results: list[dict] | None = None
) -> None:
    """Deploys the given projects one at a time, persisting progress (and
    results so far) so a Deployment Helper restart mid-run can resume where
    it left off and still include everything in the final email report."""
    remaining = list(project_ids)
    results = list(prior_results or [])
    today = date.today().isoformat()
    _save_schedule_run({"date": today, "remaining_ids": remaining, "results": results})
    _schedule_log(f"=== Scheduled run started: {len(remaining)} project(s) ===")

    while remaining:
        pid = remaining[0]
        project = get_project_map().get(pid)
        if project:
            results.append(await _scheduled_deploy_one(project))
        remaining.pop(0)
        _save_schedule_run({"date": today, "remaining_ids": remaining, "results": results})

    _clear_schedule_run()
    _schedule_log("=== Scheduled run complete ===")
    await _send_schedule_report(results)


async def _scheduler_loop() -> None:
    while True:
        await asyncio.sleep(SCHEDULER_POLL_INTERVAL)
        try:
            schedule = _load_schedule()
            hh, mm = _parse_hhmm(schedule.get("time", DEFAULT_SCHEDULE_TIME))
            if hh is None:
                continue
            tz = _parse_tz(schedule.get("timezone", DEFAULT_SCHEDULE_TZ)) or ZoneInfo(DEFAULT_SCHEDULE_TZ)
            now = datetime.now(tz)
            today_str = now.date().isoformat()
            if now.hour == hh and now.minute == mm and _load_last_trigger_date() != today_str:
                _save_last_trigger_date(today_str)
                ids = _scheduled_project_ids()
                if ids:
                    asyncio.create_task(_run_scheduled_deploys(ids))
                else:
                    _schedule_log("Scheduled time reached but no projects are opted in — skipping")
        except Exception as exc:
            _schedule_log(f"Scheduler loop error: {exc}")


@app.on_event("startup")
async def _on_startup() -> None:
    # Resume an interrupted scheduled run from earlier today — e.g. the
    # Deployment Helper's own scheduled deploy restarted this container
    # mid-run, so pick back up with whatever projects were left, carrying
    # forward results already recorded so the final email report is complete.
    progress = _load_schedule_run()
    if progress and progress.get("date") == date.today().isoformat() and progress.get("remaining_ids"):
        remaining = progress["remaining_ids"]
        prior_results = progress.get("results", [])
        _schedule_log(f"Resuming interrupted scheduled run: {len(remaining)} project(s) left")
        asyncio.create_task(_run_scheduled_deploys(remaining, prior_results))
    else:
        _clear_schedule_run()

    asyncio.create_task(_scheduler_loop())

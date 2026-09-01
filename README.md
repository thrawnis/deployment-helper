# Deployment Dashboard

A self-hosted web dashboard for managing and deploying Dockerized projects on a Linux host. Built with FastAPI and vanilla JS — no framework dependencies.

## Features

- **Project cards** — one card per project showing current git branch, last commit, last deploy time, and run time
- **One-click deploy** — runs the configured script and streams output live via Server-Sent Events
- **Deploy history** — last 5 deploys per project stored persistently, with full log output
- **Docker logs** — view the last 50 lines of `docker compose logs api` for any project inline
- **Download logs** — save deploy output or Docker logs as a `.txt` file
- **Safe self-redeploy** — redeploying the dashboard itself builds the new image first, then hands the container swap off to the Docker host so the process survives the container going down. The UI polls for the service coming back and reloads automatically
- **Deploy guards** — only one deploy runs at a time per project; the Deployment Helper blocks all other deploys while it's redeploying, and vice versa
- **Leave warning** — the browser prompts before closing/navigating away during an active deploy

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, uvicorn |
| Frontend | Vanilla HTML/CSS/JS (no framework) |
| Streaming | Server-Sent Events (SSE) |
| Container | Docker + Docker Compose |

## Project Configuration

Projects are configured via a `.env` file using numbered entries:

```env
PROJECT_1_NAME=My App
PROJECT_1_PATH=/media/docker/my-app
PROJECT_1_SCRIPT=./rebuild.sh

PROJECT_2_NAME=Other App
PROJECT_2_PATH=/media/docker/other-app
PROJECT_2_SCRIPT=./rebuild.sh
```

Copy `.env.example` to `.env` and fill in your projects. Projects are displayed alphabetically, with **Deployment Helper** always pinned last if present.

## Setup

### 1. Clone and configure

```bash
git clone <repo-url> /media/docker/deployment-helper
cd /media/docker/deployment-helper
cp .env.example .env
# Edit .env with your projects
```

### 2. Add volume mounts

For each project path in your `.env`, add a matching volume mount in `docker-compose.yml`:

```yaml
volumes:
  - /media/docker/my-app:/media/docker/my-app
```

Or mount the entire parent directory if all projects share a common root:

```yaml
volumes:
  - /media/docker:/media/docker
```

### 3. Deploy

```bash
./rebuild.sh
```

The dashboard will be available at `http://localhost:8089`.

## Adding This App to Its Own Dashboard

Add an entry to `.env`:

```env
PROJECT_N_NAME=Deployment Helper
PROJECT_N_PATH=/media/docker/deployment-helper
PROJECT_N_SCRIPT=./rebuild.sh
```

> **Note:** The deploy button for Deployment Helper is disabled while any other deploy is running, and all other deploy buttons are disabled while Deployment Helper is redeploying.

## Data & Logs

Deploy history is stored in `./data/history.json` (a bind-mounted folder alongside the project). This file is gitignored and persists across container restarts.

## Security

- Exposed via Cloudflare Tunnel + Cloudflare Access — no additional auth layer inside the app
- Only executes scripts explicitly listed in `.env` — no arbitrary command execution
- The Docker socket is mounted read-write to allow deploy scripts to run `docker`/`docker compose` commands

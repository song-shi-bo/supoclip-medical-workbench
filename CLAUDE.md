# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## DSH Runtime (3445)

Start with [DSH_STARTUP.md](DSH_STARTUP.md) for the DSH environment. The current frontend uses port 3107; Docker deployment instructions refer to a separate Docker-capable host. Verify existing services before starting or replacing them. Preserve incomplete work after a failed model turn.

## Project Overview

SupoClip is an open-source alternative to OpusClip — an AI-powered video clipping tool that transforms long-form content into viral short clips. AGPL-3.0 licensed.

## 2026-10-30: Legacy auto-clip pipeline (A-line) removed

The original SupoClip pipeline is **deleted**: ARQ worker (`src/workers/tasks.py`,
`src/worker_main.py`, `src/workers/progress.py`), legacy routes
(`src/api/routes/tasks.py`, `POST /upload`), legacy services
(`task_service.py`, `video_service.py`, `clip_editor.py`, `youtube_utils.py`,
`apify_youtube_downloader.py`, `task_completion_email_service.py`), the legacy
monolith `src/main.py`, repositories for tasks/clips/sources/cache, their
SQLAlchemy models, and the DB tables (`tasks`, `generated_clips`, `sources`,
`processing_cache`). Legacy frontend pages (`/tasks`, `/list`, `/share`,
`/api/tasks/*`, `/api/share/*`, marketing home) were also removed; the root
path now redirects to `/studio`.

The **only** editing pipeline is the Studio line: `/studio` → `studio_jobs`
(R2/local upload) → Studio job worker → `autoedit_runner_video_brain.py` →
video-brain (:8940): preflight → transcribe → medical correction → rules →
planning (hooks/themes/quotes) → render → QC.

Still shared and kept: `ai.py` (Studio copilot/planning/cover), `video_utils.py`,
`caption_templates.py`, `emoji_captions.py`, `clip_source_map.py`, `broll.py`,
`workers/job_queue.py` (Redis pool), `main_refactored.py` (API entry served by
`studio_server.py`).

## Development Commands

### Docker (recommended)

```bash
docker-compose up -d              # Start all 5 services
docker-compose up -d --build      # Rebuild after changes
docker-compose logs -f backend    # Debug backend
docker-compose logs -f worker     # Debug video processing
docker-compose down               # Stop all services
```

Services: Frontend (:3000), Backend API (:8000, docs at /docs), Worker (ARQ), PostgreSQL (:5432), Redis (:6379)

### Backend (local)

Uses `uv` (not pip/poetry). Requires Python 3.11+, ffmpeg, running PostgreSQL and Redis.

```bash
cd backend
uv venv .venv && source .venv/bin/activate
uv sync

# API server (uses refactored entry point)
uvicorn src.main_refactored:app --reload --host 0.0.0.0 --port 8000

# Worker process (required for video processing)
arq src.workers.tasks.WorkerSettings
```

### Frontend (local)

```bash
cd frontend
pnpm install
pnpm run dev          # Dev server with Turbopack
pnpm run build        # Prisma generate + Next.js build
pnpm run lint
```

### Tests

Existing suites include backend pytest, frontend Vitest, and Playwright. See `backend/README.md`, `backend/pyproject.toml`, `frontend/package.json`, and `docs/development.md` for commands and prerequisites. Database-backed tests require a dedicated test database. Run only the scope requested or required by the current task.

## Architecture

### System Overview (2026-10-30, Studio line only)

```
User → /studio (Next.js) → Studio API (main_refactored:app via studio_server)
        → studio_jobs (Postgres leases) → Studio job worker (subprocess, heartbeat)
        → tasks/in/autoedit_*.json (file queue) → autoedit_runner_video_brain.py
        → video-brain :8940 (/v1/tasks): preflight → transcribe → medical correct
          → rules → planning (hooks/themes/quotes) → render N clips → QC
        → artifacts back to Studio (R2/filesystem) → manual timeline / export → 投递
```

Backend layout that remains:

```
api/routes/          → HTTP handlers (jobs.py, transcripts.py, media.py, admin.py, ...)
services/            → api_key/billing/email services (no clip pipeline)
repositories/        → api_key_repository.py only
workers/             → job_queue.py (Redis/arq pool, used by Studio stores)
studio_*.py          → Studio server, job store/worker, copilot, workspace, access
```

**Key patterns:**
- Studio job state lives in `studio_jobs` (Postgres leases + heartbeat), artifacts in `studio_artifacts` pointing at R2/filesystem keys
- Blocking operations wrapped in `run_in_thread()`; the worker runs each task in an isolated subprocess with a 3600s cap
- Progress via `tasks/status/*.json` polled by the frontend (no Redis SSE anymore)
- Auto-edit is a file-queue contract (`tasks/in|processing|completed|failed`) between Studio and the video-brain runner

### Video Processing Pipeline

1. **Input** → Studio project upload (R2 session or local, resumable chunks)
2. **Transcription** → faster-whisper / volc ASR; Studio queue transcribe is reused by video-brain
3. **Medical correction** → homophone lexicon + optional LLM pass with similarity safety gate
4. **Planning** → video-brain `planning_prompt.py`: theme types, quote evidence threshold 0.7, timestamps validated; auto k=1-4
5. **Rendering** → video-brain renders `clip_final.mp4` + `cover.jpg` + `clip_plan.json`; QC stage; results restored into the Studio timeline

### Frontend Architecture

- **Next.js 15** with App Router, React 19, TailwindCSS v4
- **ShadCN UI** (New York style, stone base color, Radix primitives)
- **Better Auth** with Prisma adapter for email/password auth
- **No global state library** — React hooks only (`useState`, `useEffect`, `useSession`)
- All pages use `"use client"` — SSR is minimal
- Prisma client generated to `frontend/src/generated/prisma/` (custom output path)
- Build: `prisma generate && next build` (Prisma generate runs on both build and postinstall)

**Auth flow:** Frontend calls Better Auth → session cookie → passes `user_id` header to backend API

### Database

PostgreSQL 15. Schema in `init.sql`. Mixed naming conventions:
- `tasks`, `sources`, `generated_clips` → snake_case
- `session`, `account`, `verification`, `users` → camelCase (Better Auth)
- UUIDs stored as VARCHAR(36)
- Auto-update triggers on `updated_at`/`updatedAt` columns

## Key Backend Files

| File | Purpose |
|------|---------|
| `src/main_refactored.py` | FastAPI entry point (Studio + shared routes; served by `studio_server.py`) |
| `src/api/routes/jobs.py` | Studio job pipeline API: R2 upload sessions, job submit/progress, artifacts, executor endpoints |
| `src/api/routes/transcripts.py` | Studio transcript retrieval/revisions |
| `src/api/routes/media.py` | Fonts, caption templates, B-roll status, `/studio/transcribe` |
| `src/studio_job_worker.py` / `src/studio_supervisor.py` | Studio worker process + container supervisor |
| `src/autoedit_runner_video_brain.py` | File-queue executor bridging Studio → video-brain (:8940) |
| `src/studio_copilot.py` / `src/timeline_copilot.py` | LLM editing assistant (uses `ai.py`) |
| `src/ai.py` | Pydantic AI agents/models shared by copilot, planning, cover render |
| `src/video_utils.py` | Shared video/subtitle helpers (used by Studio worker, clip_transform, media routes) |
| `src/caption_templates.py` / `src/emoji_captions.py` | Caption template system (imported by video_utils) |
| `src/medical_lexicon.py` / `src/project_glossary.py` | Medical correction lexicons |
| `src/broll.py` | Pexels API B-roll integration |
| `src/config.py` | Environment variable configuration |

## API Endpoints (routes in `api/routes/`)

**Studio job pipeline (`jobs.py`, mounted with studio access enforcement):**
- `POST /api/uploads` + `/api/uploads/{id}/complete` — R2/local resumable upload sessions
- `POST /api/jobs` — create Studio jobs (transcribe/auto-edit/re-edit)
- `GET /api/jobs/{id}` — job status/progress; artifact download endpoints
- `/api/internal/jobs/*` — claim/activate/artifacts/state for the worker and runner

**Media (`media.py`):**
- `GET /fonts`, `GET /fonts/{name}`, `POST /fonts/upload`, `DELETE /fonts/{name}`
- `GET /caption-templates`, `GET /broll/status`
- `POST /studio/transcribe`, `/studio/transcribe/start`, `/studio/transcribe/chunk` — Studio transcription entry points

**Admin/feedback/billing/api-keys/transcripts/access routers** remain as before;
the legacy task endpoints (`/start-with-progress`, `/tasks/*`, `/upload`,
`/clips/{filename}`) were removed on 2026-10-30 together with the A-line.

**API keys (programmatic access):**
- `GET /api-keys/` — List the user's API keys (metadata only)
- `POST /api-keys/` — Create a key (plaintext `sk_...` returned exactly once)
- `DELETE /api-keys/{key_id}` — Revoke a key

API keys authenticate `/fonts` and `/studio/transcribe` directly via
`Authorization: Bearer sk_...` or `x-api-key`. Resolution lives in
`auth_headers.resolve_authenticated_user_id` (API key → DB lookup, else falls
back to the frontend's HMAC-signed session headers). Only the SHA-256 hash is
stored (`api_keys` table). The frontend manages keys at `/settings/api-keys`.

## Environment Variables

Required in `.env` (root) or `backend/.env`:

```bash
ASSEMBLY_AI_API_KEY=...              # Required: video transcription
LLM=google-gla:gemini-3-flash-preview # Format: provider:model-name
GOOGLE_API_KEY=...                   # Or OPENAI_API_KEY / ANTHROPIC_API_KEY
OLLAMA_BASE_URL=http://localhost:11434/v1  # Optional for ollama:* models
OLLAMA_API_KEY=...                   # Optional; required for Ollama Cloud

# Optional
PEXELS_API_KEY=...                   # B-roll stock footage
REDIS_HOST=localhost                 # Default: localhost
REDIS_PORT=6379                      # Default: 6379
QUEUED_TASK_TIMEOUT_SECONDS=180      # Fail-safe for stuck tasks
TEMP_DIR=/tmp                        # Temp file storage
DATABASE_URL=postgresql+asyncpg://...
BETTER_AUTH_SECRET=...               # Frontend auth secret
```

## Common Workflows

### Adding fonts/transitions

Drop `.ttf` files into `backend/fonts/` or `.mp4` files into `backend/transitions/`. They auto-appear via their respective `GET` endpoints.

### Modifying AI clip selection

Edit `backend/src/ai.py`: `simplified_system_prompt` controls selection criteria, `TranscriptSegment` defines the output model, `get_most_relevant_parts_by_transcript()` runs analysis with validation.

### Video processing constraints

- Output: 9:16 vertical format, H.264, even pixel dimensions (`round_to_even()`)
- Subtitles positioned at 75% down the frame
- Virality scoring: `hook_score`, `engagement_score`, `value_score`, `shareability_score` (0-25 each, summed to `virality_score` 0-100)
- Each segment gets an AI-written `hook_title` (3-9 words) burned into the top safe area for the first ~4s (`build_hook_title_ass` in `video_utils.py`), persisted on `generated_clips.hook_title`
- Static talking-head crops get a slow ~5% Ken Burns punch-in (`kenburns_zoom_fragment`); tracked pans and split screens keep their own motion

## iOS App

A native iOS app ships on the App Store
(https://apps.apple.com/us/app/supoclip/id6784760040, app id `6784760040`). Its
source lives outside this repo. It talks to the hosted API and bills through
RevenueCat, which is what `frontend/src/app/api/billing/revenuecat-webhook/`
serves; App Store subscribers are blocked from Stripe checkout/portal by the
"managed through the App Store" guard in the billing routes. The www references
the app via `APP_STORE_ID`/`APP_STORE_URL` in `frontend/src/lib/site.ts`
(Smart App Banner meta, JSON-LD `MobileApplication`, hero badge, footer link).

## MCP Server

`mcp/` is a standalone [MCP](https://modelcontextprotocol.io) server
(`supoclip-mcp`, Python/FastMCP, stdio) that exposes SupoClip to MCP clients
(Claude Desktop/Code, Cursor, …). It is a thin client over the REST API.

- **Default target:** the hosted API `https://api.supoclip.com`. Override with
  `SUPOCLIP_API_URL` for self-hosting (e.g. `http://localhost:8000`).
- **Auth:** a per-user API key in `SUPOCLIP_API_KEY` (see API keys above).
  Self-hosters may instead use `SUPOCLIP_USER_ID` (+ `SUPOCLIP_AUTH_SECRET` when
  signing is enforced).
- **Tools:** create/list/get/wait/cancel/resume/delete tasks, list/download/
  export clips, and public discovery (templates, transitions, fonts, B-roll).
- Run with `cd mcp && uv run supoclip-mcp`. Details in `mcp/README.md`.

## Documentation Maintenance — docs/AUTOEDIT_WORKBENCH.md

工作台功能/流程改动必须同步更新 `docs/AUTOEDIT_WORKBENCH.md`：改叙述章节 + 跑 `python3 docs/generate_workbench_doc.py` 刷新快照 + 同 commit 提交。收到「更新工作台文档」类任务时，先对比快照里的代码版本与 `git log <版本>..HEAD` 的差异再改写（新会话无上下文也能完成）。快照区块勿手改。详见 AGENTS.md 同名章节。

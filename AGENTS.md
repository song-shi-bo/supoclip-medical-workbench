# Repository Guidelines

## DSH Runtime (3445)

Read [DSH_STARTUP.md](DSH_STARTUP.md) first when working in this DSH workspace. It records the actual ports, personal tool paths, service checks, and screenshot output directories. Docker commands below apply to a separate Docker-capable development host; they are not the startup path inside the member container.

## Project Structure & Module Organization
This repository is a monorepo with three apps:
- `backend/`: FastAPI + async worker code (`src/api`, `src/services`, `src/repositories`, `src/workers`).
- `frontend/`: main Next.js app (`src/app`, `src/components`, `src/lib`, `prisma/`).
- `waitlist/`: separate Next.js marketing/waitlist app.

Infra and bootstrap files live at the root: `docker-compose.yml`, `init.sql`, `.env.example`, and `start.sh`.

## Build, Test, and Development Commands
Use Docker for full-stack development:
- `docker-compose up -d --build`: start frontend, backend, worker, Postgres, and Redis.
- `docker-compose logs -f`: stream service logs.
- `docker-compose down`: stop everything.

Local app commands:
- `cd frontend && npm run dev` (or `waitlist`): run Next.js in dev mode.
- `cd frontend && npm run build && npm run start`: production build + serve.
- `cd frontend && npm run lint` (same in `waitlist`): run ESLint.
- `cd backend && uv sync && uvicorn src.main_refactored:app --reload --host 0.0.0.0 --port 8000`: run API locally after its dependencies are available.
- `cd backend && .venv/bin/arq src.workers.tasks.WorkerSettings`: run the worker.

## Coding Style & Naming Conventions
- Python: 4-space indentation, type hints where practical, `snake_case` for functions/modules.
- TypeScript/React: 2-space indentation, `PascalCase` for component names, `camelCase` for variables/functions, route files in Next.js App Router conventions (`app/.../page.tsx`, `route.ts`).
- Linting: Next.js ESLint configs in `frontend/eslint.config.mjs` and `waitlist/eslint.config.mjs`.
- Imports: use the `@/*` alias in Next.js apps when possible.

## Testing Guidelines
The repository has backend pytest, frontend Vitest, and Playwright suites. Check the current package scripts, backend README, and dependency requirements before running the relevant suite:
- Run `npm run lint` in both Next.js apps.
- Smoke test core flows with `docker-compose` (create task, process clips, view task page).

When adding tests, place them near code or under `tests/` with clear names (`test_*.py`, `*.test.ts[x]`).

## Commit & Pull Request Guidelines
Recent history favors short imperative commit subjects (`Add list endpoint`, `Fix typo`, `improve UX`). Prefer:
- `type(scope): concise summary` (example: `feat(backend): add task list pagination`).
- One logical change per commit.

PRs should include:
- What changed and why.
- Any env/config or migration impact.
- Screenshots/GIFs for UI changes.
- Linked issue(s) and manual verification steps.

## Security & Configuration Tips
- Never commit real secrets; use `.env.example` as the template.
- Required runtime keys include `ASSEMBLY_AI_API_KEY` and either one hosted LLM provider key (`OPENAI_API_KEY`, `GOOGLE_API_KEY`, or `ANTHROPIC_API_KEY`) or an Ollama model configuration (`LLM=ollama:*`, optional `OLLAMA_BASE_URL`).

## Documentation Maintenance — docs/AUTOEDIT_WORKBENCH.md (工作台全景文档)

`docs/AUTOEDIT_WORKBENCH.md` 是工作台功能与自动剪辑流程的单一入口文档。改工作台功能时**必须同步维护它**，规则：

1. **每次涉及工作台功能的改动**（新增/修改路由、worker 阶段、Inspector 标签、流程行为、服务）：
   - 更新文档中对应章节的**叙述内容**（功能说明/流程步骤/原因），不要只依赖快照；
   - 跑 `python3 docs/generate_workbench_doc.py` 刷新文末「代码事实快照」；
   - 与代码同一个 commit 提交。
2. **收到的任务是「更新工作台文档」**时：先看快照里的「代码版本」与 `git log <该版本>..HEAD --oneline` 的差异，据此重写受影响章节——新窗口/新会话没有对话上下文也能照此完成。
3. 快照区块（`BEGIN/END:AUTO-SNAPSHOT` 之间）由生成器自动维护，**不要手改**；`--check` 过期时后端测试 `test_doc_workbench_snapshot` 会红。

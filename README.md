# Orchestra

Open-source multi-agent AI orchestration system. Define tasks in a web console, let an architect assign them to workers, review results automatically, and merge code — all coordinated through a shared SQLite database.

```
docker compose up
```

Open **http://localhost:8201** for the web console.

## Architecture

```
                    +-------------+
                    |   Web SPA   |  :8201 (nginx)
                    +------+------+
                           |
                    +------v------+
                    | FastAPI API  |  :8200 (uvicorn)
                    +------+------+
                           |
                   +-------v--------+
                   | SQLite WAL DB  |
                   +-------+--------+
                           |
                  +--------v---------+
                  |   Orchestrator   |  (async Python)
                  +--+----+----+----+
                     |    |    |    |
                    A    W1   W2   R     <-- Claude CLI / Ollama
                    |    |    |    |
              Architect  Workers  Reviewer
```

**Three services** in Docker Compose:

| Service | Port | Description |
|---|---|---|
| `api` | 8200 | FastAPI + SQLite. Task CRUD, locks, events, architect chat |
| `web` | 8201 | Vanilla JS SPA served by nginx, proxies `/v1/` to api |
| `orchestrator` | — | Async loop: architect plans, workers execute, reviewer validates |

### Roles

- **Planner** — reads a brief, creates fronts/objectives/tasks in the console
- **Researcher** — gathers context before execution (web search, code reading)
- **Architect** — assigns available tasks to workers, picks models, writes hints
- **Worker** — executes a single task (writes code, runs tests, commits)
- **Reviewer** — validates worker output, approves or rejects with feedback

### Architect Chat

The **Architect** tab in the web console lets you interact with the architect in real time:

- Send messages (instructions, priorities, questions)
- See the architect's reasoning and assignments
- Approve or reject assignments manually (when `ARCHITECT_AUTO_APPROVE=false`)
- Trigger an architect cycle on demand

Messages are delivered to the architect as context on its next cycle via SSE streaming.

## Quick Start

1. **Clone and configure:**
   ```bash
   git clone https://github.com/yourorg/orchestra.git
   cd orchestra
   cp .env.example .env
   # Edit .env — at minimum set FRENTES
   ```

2. **Start services:**
   ```bash
   docker compose up -d
   ```

3. **Create a front and tasks** in the web console at http://localhost:8201, or use the API directly:
   ```bash
   curl -X POST http://localhost:8200/v1/frentes \
     -H 'Content-Type: application/json' \
     -d '{"slug":"demo","nombre":"Demo","repo_path":"/workspace/demo"}'
   ```

4. **Brief mode** — give the orchestrator a markdown brief and let it plan + execute:
   ```bash
   docker compose run orchestrator python __main__.py --brief /briefs/demo-todo.md
   ```

## Configuration

All configuration is via environment variables. See `.env.example` for the full list.

### Core

| Variable | Default | Description |
|---|---|---|
| `ORCHESTRA_DB` | `/data/orchestra.db` | SQLite database path |
| `CONSOLE_URL` | `http://api:8200` | API URL (for orchestrator) |
| `FRENTES` | _(empty)_ | Comma-separated front slugs to orchestrate |
| `MAX_WORKERS` | `2` | Max concurrent workers |
| `MAX_BUDGET_USD` | `10.0` | Total budget cap |
| `WORKER_BUDGET_USD` | `2.0` | Budget per worker invocation |
| `REVIEWER_BUDGET_USD` | `1.0` | Budget per review |
| `ARCHITECT_BUDGET_USD` | `1.5` | Budget per architect cycle |
| `DRY_RUN` | `false` | Log decisions without executing |

### Models

| Variable | Default | Description |
|---|---|---|
| `ARCHITECT_MODEL` | `opus` | Model for the architect role |
| `WORKER_MODEL` | `sonnet` | Model for workers |
| `REVIEWER_MODEL` | `sonnet` | Model for the reviewer |
| `ARCHITECT_AUTO_APPROVE` | `true` | Auto-execute assignments (false = manual approval in web) |

### Ollama (optional)

Run with local LLMs instead of Claude. Start with `docker compose --profile ollama up`.

| Variable | Default | Description |
|---|---|---|
| `LLM_BACKEND` | `claude` | `claude` or `ollama` |
| `OLLAMA_URL` | `http://ollama:11434` | Ollama API endpoint |
| `OLLAMA_ARCHITECT_MODEL` | `llama3.1:70b` | Ollama model for architect |
| `OLLAMA_WORKER_MODEL` | `codellama:34b` | Ollama model for workers |
| `OLLAMA_REVIEWER_MODEL` | `llama3.1:8b` | Ollama model for reviewer |

> **Note:** Ollama models cannot execute tools (file editing, git, bash). Workers running on Ollama generate text-only output — they cannot modify files directly. For production use, keep workers on Claude and use Ollama for architect/reviewer only (hybrid mode requires two runner instances, not yet implemented).

### Other

| Variable | Default | Description |
|---|---|---|
| `WORKSPACE_DIR` | `/workspace` | Directory where repos are cloned |
| `GITHUB_ORG` | _(empty)_ | GitHub org for auto-cloning |
| `REPO_MAP` | _(empty)_ | `frente=repo,frente2=repo2` mapping |
| `INTEGRATION_BRANCH` | _(empty)_ | Branch for merging completed tasks |
| `DOCKER_TARGET` | _(empty)_ | URL for e2e testing containers |
| `WORKER_MAX_TURNS` | `15` | Max Claude CLI turns per worker |
| `LOCK_TTL_MIN` | `45` | Lock expiration in minutes |

## Writing Briefs

A brief is a markdown file describing what you want built. The orchestrator's planner reads it and creates fronts, objectives, and tasks automatically.

```markdown
# My Project

Build a REST API for managing bookmarks.

## Requirements
- CRUD endpoints for bookmarks (title, url, tags)
- SQLite storage
- Search by tag

## Technical constraints
- Python + FastAPI
- Single file: main.py
- Include tests

## Acceptance criteria
- POST /bookmarks creates a bookmark
- GET /bookmarks?tag=python filters by tag
- Tests pass with pytest
```

Place briefs in the `briefs/` directory and run:
```bash
docker compose run orchestrator python __main__.py --brief /briefs/my-brief.md
```

See `briefs/demo-todo.md` and `briefs/demo-calculator.md` for examples.

## API Reference

The API runs on port 8200. All endpoints are under `/v1/`.

### Fronts
- `GET /v1/frentes` — list fronts
- `POST /v1/frentes` — create front
- `PATCH /v1/frentes/{slug}` — update front

### Tasks
- `GET /v1/tareas` — list tasks (filter by `frente`, `estado`)
- `POST /v1/tareas` — create task
- `PATCH /v1/tareas/{codigo}` — update task
- `DELETE /v1/tareas/{codigo}` — delete task

### Locks
- `POST /v1/locks` — acquire lock on a task
- `DELETE /v1/locks/{id}` — release lock
- `PATCH /v1/locks/{id}/renew` — renew lock TTL

### Events
- `GET /v1/eventos` — list events
- `GET /v1/eventos/stream?tarea_codigo=X` — SSE stream for a task
- `POST /v1/eventos` — create event

### Architect Chat
- `POST /v1/architect/messages` — send message
- `GET /v1/architect/messages` — list messages
- `GET /v1/architect/messages/stream` — SSE stream
- `POST /v1/architect/assignments` — create assignment
- `GET /v1/architect/assignments` — list assignments
- `PUT /v1/architect/assignments/{id}` — approve/reject
- `POST /v1/architect/invoke` — trigger architect
- `GET /v1/architect/config` — current config

### Other
- `GET /v1/stats` — dashboard stats
- `GET /v1/plan` — master plan view
- `GET /v1/objetivos` — objectives
- `GET /v1/sesiones` — sessions
- `GET /v1/skills` — skills catalog
- `GET /v1/biblioteca` — library
- `GET /v1/diario` — journal

## Adding a New LLM Backend

1. Create `orchestrator/my_runner.py` implementing the `LLMRunner` ABC from `llm_runner.py`
2. Implement the `run()` method returning an `LLMResult`
3. Set `supports_tools = True` if your backend can execute tools
4. Add your backend to the `create_runner()` factory in `llm_runner.py`
5. Add config vars to `config.py` and `.env.example`

## License

MIT

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted AI agent stack built on a single machine. Two cooperating parts:

- **The agent** — the LLM persona. Runs natively under Ollama (qwen3 or compatible model). Identity and behavioral rules go in `AGENTS.md` (gitignored — copy from `AGENTS.md.example`).
- **Lumina API** — the tool layer. A FastAPI service (`lumina-api/`) that sits between Open WebUI and Ollama. Open WebUI talks OpenAI-compatible to `lumina-api:/v1/chat/completions`; the endpoint injects the system prompt + tools and drives a multi-round tool-call loop against Ollama.

Postgres holds structured data. ChromaDB holds embeddings. SearXNG is the private web-search backend.

## Common commands

```bash
docker compose up -d                             # start everything
docker compose restart lumina-api                # after Python edits (uvicorn --reload also handles hot-reload)
docker compose logs -f lumina-api                # tail API logs incl. ---- LLM ROUND / ---- TOOL CALL trace lines
docker compose logs lumina-api | grep "\[CRON\]" # APScheduler activity

bash up_test.sh                                  # smoke-test all services
bash bench.sh <label>                            # 3-run generation benchmark
```

### Database

Schema lives in `lumina-api/db/migrations/init.sql`, bind-mounted into Postgres — runs only on first container boot. Forward changes go in numbered migrations (`001_*.sql`, `002_*.sql`, …). Migrations are forward-only.

```bash
# Apply a migration
docker compose exec -T postgres bash -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1' < lumina-api/db/migrations/00X_xxx.sql

# Inspect schema
docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB -c "\dt"
```

### Ollama

Ollama runs **natively (systemd), not in Docker**. The compose file uses `extra_hosts: host.docker.internal:host-gateway` so containers can reach it at `http://host.docker.internal:11434`. Ollama must listen on `0.0.0.0`.

```bash
ollama create lumina-prod -f Modelfile.lumina-prod
```

## Architecture

### Inference path

`routers/openai_compat.py` is the heart of the system. Every Open WebUI message hits `POST /v1/chat/completions`:

1. **Model override** — `/reason` or `/fast` prefixes swap the model tag.
2. **System prompt injection** — composes `AGENTS.md` with any matched mode prompts from `lumina-api/agents/<mode>.md`.
3. **Tool registration** — `services/tools.openai_schemas()` is appended as the `tools` parameter.
4. **Multi-round tool loop** — calls Ollama, dispatches `tool_calls`, appends results, loops. `MAX_TOOL_ROUNDS = 3`.

### Tool registry (`services/tools.py`)

Each tool is a `ToolSpec(name, description, parameters, handler)`. `execute()` catches all exceptions and returns them as JSON so the model can recover. Tools not in the `TOOLS` list are not exposed.

### Vault / library

Drop Markdown, PDF, or EPUB files into `files/` (your vault). The library sync job (`job_vault_sync`, every 15 min) ingests them into Postgres + ChromaDB. The agent can search and read them via the `document_search` and `file_read` tools.

### People / Synapses

Drop `Synapses/<Name>.md` files in your vault. The people sync job loads them into Postgres. The agent surfaces them during conversations when someone is mentioned by name.

### Scheduler (`scheduler.py`)

APScheduler runs in-process. Jobs post to `localhost:8000`. Active by default:
- `weather_refresh` — 00:00 UTC daily
- `sandbox_sync` — every 5 min
- `vault_sync` — every 15 min
- `people_sync` — configurable interval
- `calendar_sync` — every hour
- `gmail_sync` — 07:00 UTC daily
- `weekly_backup` — Sunday 03:00 UTC
- `reminder_check` — every minute

### System prompt

`AGENTS.md` is bind-mounted read-only into `/app/AGENTS.md`. All code paths that talk to the LLM route through `build_system_prompt` in `utils/prompts.py` so AGENTS.md stays the single source of truth.

## Persistence pattern

Pydantic schemas (`schemas/<vertical>.py`) at the boundary, repository functions (`repos/<vertical>.py`) running parameterized SQL on `db.postgres.engine`. No SQLAlchemy ORM. Every persistent write goes through one repo function.

## Gotchas

- **`.env` is a symlink** to `.env.prod`. Editing `.env` edits prod. Use `--env-file .env.dev` to override.
- **Ollama is not in compose.** It's a systemd service on the host. Must listen on `0.0.0.0`.
- **`uvicorn --reload` is on** with `./lumina-api` bind-mounted. Code edits hot-reload without a restart.
- **ChromaDB pinned to `0.6.3`** — 0.6.x API differs from 0.5.x; don't bump without auditing `db/chroma.py`.

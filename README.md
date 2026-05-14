# Lumina

A self-hosted AI agent that lives on your home server or mini-PC. Lumina is a tool layer — it wraps any Ollama-compatible model with a multi-round tool loop, a private web search backend, a vault-synced document library, calendar and Gmail integration, and a push notification system. You talk to it through Open WebUI; it has persistent memory of your documents, contacts, and reminders.

No cloud API keys required. Everything runs on your hardware.

---

## What it is

Open WebUI → **Lumina API** (FastAPI) → Ollama (local LLM)

The Lumina API is the agent's tool layer. It sits between Open WebUI and Ollama, injects your `AGENTS.md` system prompt, exposes tools to the model, and drives a multi-round tool-call loop so the model can act, not just chat.

**Core capabilities out of the box:**

| Feature | How it works |
|---|---|
| Web search | Private SearXNG instance — no Google tracking |
| Document library | Drop files into `files/` — Markdown, PDF, EPUB auto-ingested + embedded |
| People / contacts | `Synapses/<Name>.md` in your vault → agent references them by name |
| Google Calendar | Hourly sync → agent can read and query your schedule |
| Gmail | Daily sync → agent can search and summarize your inbox |
| Reminders | One-shot and recurring, delivered via ntfy push |
| Daily briefing | Weather + calendar + any context you configure |
| Push notifications | Self-hosted ntfy — works over Tailscale / local network |
| Scheduled backups | Weekly Postgres + sandbox dump |
| Analytics | Optional Metabase dashboard over your own data |

---

## Hardware requirements

Lumina runs comfortably on a mini-PC with **16 GB RAM** (e.g. GMKtec M6, Beelink SER series, Intel NUC). 32 GB is recommended if you want to run the 30b MoE model alongside the tool layer.

**Software:**
- Docker + Docker Compose
- Ollama (runs natively, not in Docker — better performance)
- A GPU or NPU is helpful but not required; CPU inference works fine for the 4b model

---

## Quick start

### 1. Clone and configure

```bash
git clone https://github.com/your-username/lumina.git
cd lumina
cp .env.example .env
# Edit .env — at minimum set POSTGRES_PASSWORD, WEBUI_SECRET_KEY, USER_NAME, USER_EMAIL
```

### 2. Build the agent model

Install [Ollama](https://ollama.com), then:

```bash
ollama pull qwen3:4b-instruct   # or any compatible model
ollama create lumina-prod -f Modelfile.lumina-prod
```

### 3. Set up your AGENTS.md

```bash
cp AGENTS.md.example AGENTS.md
# Edit AGENTS.md — this is the agent's system prompt and identity
```

### 4. Start everything

```bash
docker compose up -d
bash up_test.sh   # verify all services are healthy
```

Open WebUI will be available at `http://localhost:3000`.

---

## Directory structure

```
lumina/
├── AGENTS.md              # agent system prompt (gitignored — copy from .env.example)
├── AGENTS.md.example      # template
├── CLAUDE.md              # dev guidance for Claude Code
├── docker-compose.yml
├── Modelfile.lumina-prod  # Ollama model definition
├── files/                 # your vault — drop Markdown, PDF, EPUB here
├── sandbox/               # isolated Python runner workspace
├── Synapses/              # one .md per person — agent reads these as context
│   └── Example.md         # template
├── searxng/               # SearXNG config (settings.yml)
└── lumina-api/            # FastAPI app
    ├── agents/            # mode prompts (chef, planner, architect, …)
    ├── db/
    │   └── migrations/    # forward-only SQL migrations
    ├── repos/             # all DB writes go through here
    ├── routers/           # FastAPI endpoints
    ├── schemas/           # Pydantic models
    ├── services/          # business logic
    └── utils/
```

---

## Vault setup

The `files/` directory is your vault. Lumina monitors it and syncs every 15 minutes. Structure is completely up to you — subdirectories, whatever you like. Supported formats: `.md`, `.pdf`, `.epub`, `.txt`.

```
files/
├── Synapses/              # people context (symlink or subfolder — either works)
├── Notes/                 # general notes
├── Reference/             # PDFs, books, manuals
└── ...                    # anything else
```

The agent can search (`document_search`), read (`file_read`), write (`file_write`), and grep (`file_grep`) the vault.

---

## Google integrations (Calendar + Gmail)

1. Create a Google Cloud project, enable Calendar API and Gmail API
2. Create OAuth credentials (Desktop app type), download `credentials.json`
3. Place `credentials.json` at the path in your `.env` (`GOOGLE_CREDENTIALS_PATH`)
4. Run the auth flow once: `docker compose exec lumina-api python services/google_auth.py`
5. This generates `token.json` — Calendar and Gmail sync will start on next cron tick

---

## Push notifications (ntfy)

ntfy runs as a Docker service. Install the [ntfy app](https://ntfy.sh) on your phone and subscribe to your topic. Update the `--base-url` in `docker-compose.yml` to your server's IP or Tailscale hostname for external push to work.

---

## Adding verticals

The codebase is designed to be extended. To add a new vertical (e.g. a fitness tracker, expenses, movies):

1. Add a migration in `lumina-api/db/migrations/`
2. Write `repos/<vertical>.py` with parameterized SQL
3. Write `routers/<vertical>.py` with FastAPI endpoints
4. Register any tools in `services/tools/`
5. Register the router in `main.py`
6. Add a cron job to `scheduler.py` if needed

---

## Applying migrations

```bash
docker compose exec -T postgres bash -c \
  'psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1' \
  < lumina-api/db/migrations/001_ollama_inference_logs.sql
```

Run each migration file in order. Migrations are forward-only — never edit an applied migration; add a new one instead.

---

## Smoke test

```bash
bash up_test.sh
```

Checks that Postgres, ChromaDB, SearXNG, Ollama, and the Lumina API are all responding.

---

## License

[GPL](LICENSE)

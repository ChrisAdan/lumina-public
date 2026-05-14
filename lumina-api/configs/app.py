import os

# ============================================================
# LOCALE
# ============================================================
# IANA tz name used to render user-facing local times (calendar, briefing, etc.).
# Falls back to WEATHER_TIMEZONE for backward compat with deployments that only set that one.
LOCAL_TIMEZONE = os.getenv("LOCAL_TIMEZONE", os.getenv("WEATHER_TIMEZONE", "America/New_York"))
# No defaults — must be supplied via env. None = unconfigured; calendar etc. degrade gracefully.
USER_NAME  = os.getenv("USER_NAME") or None
USER_EMAIL = os.getenv("USER_EMAIL") or None

# ============================================================
# OLLAMA
# ============================================================
# Default endpoint — used for any model without a specific override.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
# Per-tier overrides. Unset = fall back to OLLAMA_URL.
# Point OLLAMA_URL_REASON / OLLAMA_URL_CODE at a cloud GPU instance to offload
# heavy models while keeping the fast 4b running locally.
OLLAMA_URL_FAST   = os.getenv("OLLAMA_URL_FAST")   or OLLAMA_URL
OLLAMA_URL_REASON = os.getenv("OLLAMA_URL_REASON") or OLLAMA_URL
OLLAMA_URL_CODE   = os.getenv("OLLAMA_URL_CODE")   or OLLAMA_URL

# ============================================================
# POSTGRES
# ============================================================
POSTGRES_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/lumina"
)

# ============================================================
# SEARXNG
# ============================================================
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080")

# ============================================================
# CHROMA
# ============================================================
CHROMA_HOST = os.getenv("CHROMA_HOST", "chroma")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))

# ============================================================
# PLAID
# ============================================================
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID", "")
PLAID_SECRET    = os.getenv("PLAID_SECRET", "")
PLAID_ENV       = os.getenv("PLAID_ENV", "sandbox")  # sandbox | development | production

# ============================================================
# TMDB (Movies synapse enrichment — https://www.themoviedb.org/)
# ============================================================
# Free API key at https://www.themoviedb.org/settings/api.
# When empty, movies sync still runs but stores raw titles only —
# tools that require enriched metadata (genre/year/rating) return
# the unmatched-title set so Lumina can surface a helpful error.
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# ============================================================
# WEATHER (Open-Meteo — no API key required)
# ============================================================
WEATHER_LATITUDE      = float(os.getenv("WEATHER_LATITUDE", "40.7128"))
WEATHER_LONGITUDE     = float(os.getenv("WEATHER_LONGITUDE", "-74.0060"))
WEATHER_TIMEZONE      = os.getenv("WEATHER_TIMEZONE", "America/New_York")
WEATHER_LOCATION_NAME = os.getenv("WEATHER_LOCATION_NAME", "Home")
OPEN_METEO_URL        = "https://api.open-meteo.com/v1/forecast"

# ============================================================
# GOOGLE (Calendar, Gmail, Drive)
# ============================================================
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "secrets/credentials.json")
GOOGLE_TOKEN_PATH        = os.getenv("GOOGLE_TOKEN_PATH", "secrets/token.json")

# Calendar: how many days ahead to pull into Postgres + ChromaDB
CALENDAR_LOOKAHEAD_DAYS = int(os.getenv("CALENDAR_LOOKAHEAD_DAYS", "14"))

# Gmail: how many days back to scrape on each sync
GMAIL_LOOKBACK_DAYS = int(os.getenv("GMAIL_LOOKBACK_DAYS", "7"))

# Gmail: comma-separated Gmail label IDs to exclude (default: skip promotions + spam)
GMAIL_EXCLUDED_LABELS = os.getenv(
    "GMAIL_EXCLUDED_LABELS",
    "CATEGORY_PROMOTIONS,SPAM"
).split(",")

# Drive: ID of the folder Lumina watches for RAG ingestion
# Get from URL: drive.google.com/drive/folders/<FOLDER_ID>
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")

# ============================================================
# GITHUB (repo read access)
# ============================================================
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")

# ============================================================
# ALPACA (market data)
# ============================================================
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
# Symbols are managed in Lumina/Synapses/Financial/Symbols.md — not env vars.

# ============================================================
# NTFY (push notifications)
# ============================================================
NTFY_URL   = os.getenv("NTFY_URL", "http://ntfy:80")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "lumina-alerts")
# Notify when a response takes longer than this many seconds OR any tool fired.
# Set to 0 to notify on every response.
LUMINA_NOTIFY_MIN_SECONDS = int(os.getenv("LUMINA_NOTIFY_MIN_SECONDS", "20"))

# ============================================================
# APP
# ============================================================
ENV   = os.getenv("ENV", "development")
DEBUG = ENV == "development"

# Strip [text](url) constructs from streamed content on no-tool rounds.
# Defends against the 4b model fabricating plausible URLs to satisfy the
# citation rule when no tool was called. Toggle via env without code change.
LUMINA_STRIP_URLS = os.getenv("LUMINA_STRIP_URLS", "1").lower() in ("1", "true", "yes")

# Inject a one-line library inventory into the system prompt so the model
# knows what's available before picking a tool. Closes the routing gap
# documented in RUNBOOK Phase 10.5. Disable for A/B testing or to debug
# prompt-token budget regressions.
LUMINA_LIBRARY_INVENTORY_ENABLED = os.getenv(
    "LUMINA_LIBRARY_INVENTORY_ENABLED", "1"
).lower() in ("1", "true", "yes")

# Path to the user's Obsidian vault (markdown notes ingested into the library).
# Mounted into the container at /app/library/obsidian by the existing bind
# mount of ./lumina-api → /app. The host edits via sshfs; the scheduler
# walks this path every 15 min and re-embeds changed notes.
LUMINA_OBSIDIAN_VAULT_PATH = os.getenv(
    "LUMINA_OBSIDIAN_VAULT_PATH", "/app/library/obsidian"
)

# Sandbox directory: the vault mirror that Lumina writes to.
# Bind-mounted from ./sandbox on the host (docker-compose). The bidirectional
# sandbox_sync job (every 5 min) pushes diffs between here and the vault;
# human edits always win on conflict.
LUMINA_SANDBOX_DIR = os.getenv("LUMINA_SANDBOX_DIR", "/app/sandbox")

# Runtime people-context injection. When on, the OpenAI-compatible proxy
# appends a compact block of matched people notes when a known person is
# explicitly mentioned in the latest user message.
LUMINA_PEOPLE_CONTEXT_ENABLED = os.getenv(
    "LUMINA_PEOPLE_CONTEXT_ENABLED", "1"
).lower() in ("1", "true", "yes")

# How often the people collection sync job should run.
LUMINA_PEOPLE_SYNC_INTERVAL_MINUTES = int(
    os.getenv("LUMINA_PEOPLE_SYNC_INTERVAL_MINUTES", "5")
)

# ============================================================
# ROUTING & TRIAGE (Phase 14)
# ============================================================
# Code-specialist model tags. Register via:
#   ollama create lumina-prod-code-16b -f Modelfile.lumina-prod-code-16b
#   ollama create lumina-prod-code-2.5  -f Modelfile.lumina-prod-code-2.5
CODE_HEAVY_MODEL_TAG = os.getenv("CODE_HEAVY_MODEL_TAG", "lumina-prod-code-16b")
CODE_FAST_MODEL_TAG  = os.getenv("CODE_FAST_MODEL_TAG",  "lumina-prod-code-2.5")

# Enable the triage classifier (fast intercept model classifies intent before
# routing to the right specialist). Disable to revert to legacy /fast+/reason.
LUMINA_TRIAGE_ENABLED = os.getenv("LUMINA_TRIAGE_ENABLED", "1").lower() in ("1", "true", "yes")

# Force format="json" on the first round when intent is code_python or code_sql.
# Disabled by default: the model has tools in the request on every turn, and
# format="json" + tools causes qwen3 to emit JSON prose instead of a real
# tool_call. Re-enable only on a code-only path that strips tools from the body.
LUMINA_CODE_STRUCTURED_OUTPUT = os.getenv("LUMINA_CODE_STRUCTURED_OUTPUT", "0").lower() in ("1", "true", "yes")

# Semantic tool selection: narrow the tool list sent to the LLM to the top-k
# most relevant tools for each turn (ChromaDB cosine search + triage hints).
# Reduces prompt-token overhead as the tool registry grows.
# Set K higher if tools are being missed; lower to tighten the context budget.
LUMINA_TOOL_SELECTION_ENABLED = os.getenv("LUMINA_TOOL_SELECTION_ENABLED", "1").lower() in ("1", "true", "yes")
LUMINA_TOOL_SELECTION_K = int(os.getenv("LUMINA_TOOL_SELECTION_K", "12"))

# Context pruning (Phase 15.2): when a conversation exceeds LUMINA_PRUNE_KEEP_TURNS,
# older turns are summarized into a single system message instead of being dropped.
# This bounds prompt tokens while preserving early-conversation context.
# Set to 0 to disable (falls back to plain truncation at LUMINA_HISTORY_WINDOW).
LUMINA_PRUNE_KEEP_TURNS = int(os.getenv("LUMINA_PRUNE_KEEP_TURNS", "8"))

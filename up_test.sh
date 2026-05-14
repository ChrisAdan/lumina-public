#!/bin/bash
# Lumina M2 smoke test
# Run after: docker compose --env-file .env.dev up -d
# Usage: bash smoke_test.sh

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

pass() { echo -e "${GREEN}✅ $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; }

echo ""
echo "═══════════════════════════════════"
echo "  Lumina M2 Smoke Test"
echo "═══════════════════════════════════"
echo ""

# Lumina API health
if curl -sf http://localhost:8000/health > /dev/null; then
  pass "Lumina API is up"
else
  fail "Lumina API not responding on :8000"
fi

# Ollama reachable from host
if curl -sf http://localhost:11434/api/tags > /dev/null; then
  pass "Ollama is up (native)"
  echo "   Models: $(curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; print(', '.join(m['name'] for m in json.load(sys.stdin)['models']))" 2>/dev/null || echo 'none pulled yet')"
else
  fail "Ollama not responding on :11434 — is it running? (systemctl status ollama)"
fi

# Ollama reachable from lumina-api container
OLLAMA_STATUS=$(curl -sf http://localhost:8000/ollama/ping 2>/dev/null)
if echo "$OLLAMA_STATUS" | grep -q '"status":"ok"'; then
  pass "Ollama reachable from lumina-api container"
else
  fail "lumina-api cannot reach Ollama — check OLLAMA_URL / extra_hosts"
  echo "   Response: $OLLAMA_STATUS"
fi

# Postgres
if docker exec lumina-postgres pg_isready -U lumina > /dev/null 2>&1; then
  pass "Postgres is up"
else
  fail "Postgres not ready"
fi

# ChromaDB
if curl -sf http://localhost:8001/api/v2/heartbeat > /dev/null; then
  pass "ChromaDB is up"
else
  fail "ChromaDB not responding on :8001"
fi

# Open WebUI
if curl -sf http://localhost:3000 > /dev/null; then
  pass "Open WebUI is up — visit http://localhost:3000"
else
  fail "Open WebUI not responding on :3000 (may still be pulling image)"
fi

# SearXNG
if curl -sf "http://localhost:8080/search?q=test&format=json" > /dev/null; then
  pass "SearXNG is up and returning JSON"
else
  fail "SearXNG not responding on :8080 — check searxng/settings.yml"
fi

echo ""
echo "═══════════════════════════════════"
echo "  Done. Fix any ❌ before M3."
echo "═══════════════════════════════════"
echo ""
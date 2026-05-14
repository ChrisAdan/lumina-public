#!/bin/bash
# rebuild_db.sh
# Manual-only database rebuild script.
# Dumps the current DB, drops and recreates it, then runs init.sql fresh.
#
# Usage:
#   ./rebuild_db.sh --rebuild              # dump + wipe + reinit
#   ./rebuild_db.sh --rebuild --skip-dump  # wipe without saving a backup first
#
# Never runs automatically. Must be invoked explicitly.

set -euo pipefail

# ============================================================
# CONFIG — adjust to match your environment
# ============================================================
DB_NAME="${POSTGRES_DB}"
DB_USER="${POSTGRES_USER}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
INIT_SQL="${INIT_SQL:-./init.sql}"
DUMP_DIR="${DUMP_DIR:-./backups}"

# ============================================================
# FLAGS
# ============================================================
REBUILD=false
SKIP_DUMP=false

for arg in "$@"; do
  case $arg in
    --rebuild)    REBUILD=true ;;
    --skip-dump)  SKIP_DUMP=true ;;
    *)
      echo "Unknown argument: $arg"
      echo "Usage: $0 --rebuild [--skip-dump]"
      exit 1
      ;;
  esac
done

# ============================================================
# GUARD: must explicitly pass --rebuild
# ============================================================
if [ "$REBUILD" = false ]; then
  echo ""
  echo "  Nothing to do. This script requires --rebuild to run."
  echo "  Usage: $0 --rebuild [--skip-dump]"
  echo ""
  exit 0
fi

# ============================================================
# CONFIRM
# ============================================================
echo ""
echo "  WARNING: This will DESTROY and RECREATE the '$DB_NAME' database."
echo "  All data will be lost unless a dump is taken first."
echo ""
read -p "  Type 'yes' to continue: " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
  echo "  Aborted."
  exit 0
fi

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
PG_OPTS="-h $DB_HOST -p $DB_PORT -U $DB_USER"

# ============================================================
# STEP 1: DUMP
# ============================================================
if [ "$SKIP_DUMP" = false ]; then
  mkdir -p "$DUMP_DIR"
  DUMP_FILE="$DUMP_DIR/${DB_NAME}_backup_${TIMESTAMP}.sql"
  echo ""
  echo "  Dumping '$DB_NAME' to $DUMP_FILE ..."
  pg_dump $PG_OPTS "$DB_NAME" > "$DUMP_FILE"
  echo "  Dump saved: $DUMP_FILE"
else
  echo ""
  echo "  Skipping dump (--skip-dump passed)."
fi

# ============================================================
# STEP 2: DROP + RECREATE
# ============================================================
echo ""
echo "  Dropping database '$DB_NAME' ..."
psql $PG_OPTS -d postgres -c "DROP DATABASE IF EXISTS $DB_NAME;"

echo "  Creating database '$DB_NAME' ..."
psql $PG_OPTS -d postgres -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"

# ============================================================
# STEP 3: RUN INIT SQL
# ============================================================
echo ""
echo "  Running $INIT_SQL ..."
psql $PG_OPTS -d "$DB_NAME" -f "$INIT_SQL"

echo ""
echo "  Rebuild complete. Database '$DB_NAME' is fresh."
echo "  Timestamp: $TIMESTAMP"
if [ "$SKIP_DUMP" = false ]; then
  echo "  Backup at: $DUMP_FILE"
fi
echo ""

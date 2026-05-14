"""Weekly backup — Postgres dump + sandbox snapshot with 4-week rotation.

Outputs two files per run into LUMINA_BACKUP_DIR (/app/backups by default,
bind-mounted to ./backups on the host):

  lumina_db_YYYY-MM-DD.sql.gz      — full pg_dump, gzip-compressed
  lumina_sandbox_YYYY-MM-DD.tar.gz — vault sandbox mirror (markdown/text only)

Rotates to keep the LUMINA_BACKUP_KEEP most-recent of each type (default 4).
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("lumina.backup")

BACKUP_DIR = Path(os.getenv("LUMINA_BACKUP_DIR", "/app/backups"))
SANDBOX_DIR = Path(os.getenv("LUMINA_SANDBOX_DIR", "/app/sandbox"))
KEEP_LAST_N = int(os.getenv("LUMINA_BACKUP_KEEP", "4"))


def _postgres_url() -> str:
    return os.getenv("POSTGRES_URL", "")


async def _pg_dump(dest: Path) -> int:
    """Run pg_dump against POSTGRES_URL, gzip the output. Returns raw byte count."""
    url = _postgres_url()
    if not url:
        raise RuntimeError("POSTGRES_URL not set")
    proc = await asyncio.create_subprocess_exec(
        "pg_dump", url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump exited {proc.returncode}: {stderr.decode()[:400]}")
    with gzip.open(dest, "wb", compresslevel=6) as f:
        f.write(stdout)
    return len(stdout)


def _snapshot_sandbox(dest: Path) -> int:
    """Tar+gz the sandbox directory. Returns total uncompressed bytes."""
    if not SANDBOX_DIR.exists():
        log.warning("[backup] sandbox dir missing, skipping: %s", SANDBOX_DIR)
        return 0
    total = 0
    with tarfile.open(dest, "w:gz") as tar:
        for p in sorted(SANDBOX_DIR.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=str(p.relative_to(SANDBOX_DIR)))
                total += p.stat().st_size
    return total


def _rotate(prefix: str) -> None:
    """Delete oldest files matching prefix* until only KEEP_LAST_N remain."""
    files = sorted(BACKUP_DIR.glob(f"{prefix}*"), key=lambda p: p.stat().st_mtime)
    for old in files[:-KEEP_LAST_N]:
        old.unlink(missing_ok=True)
        log.info("[backup] rotated out: %s", old.name)


async def run_backup() -> dict:
    """Run full backup. Returns a result dict; never raises."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result: dict = {"date": stamp, "errors": []}

    pg_dest = BACKUP_DIR / f"lumina_db_{stamp}.sql.gz"
    try:
        raw = await _pg_dump(pg_dest)
        gz_size = pg_dest.stat().st_size
        _rotate("lumina_db_")
        result["postgres"] = {"file": pg_dest.name, "raw_bytes": raw, "gz_bytes": gz_size}
        log.info("[backup] postgres → %s  raw=%dKB gz=%dKB", pg_dest.name, raw // 1024, gz_size // 1024)
    except Exception as e:
        result["errors"].append(f"postgres: {e}")
        log.error("[backup] postgres dump failed: %s", e)

    sandbox_dest = BACKUP_DIR / f"lumina_sandbox_{stamp}.tar.gz"
    try:
        raw = _snapshot_sandbox(sandbox_dest)
        gz_size = sandbox_dest.stat().st_size if sandbox_dest.exists() else 0
        _rotate("lumina_sandbox_")
        result["sandbox"] = {"file": sandbox_dest.name, "raw_bytes": raw, "gz_bytes": gz_size}
        log.info("[backup] sandbox → %s  raw=%dKB gz=%dKB", sandbox_dest.name, raw // 1024, gz_size // 1024)
    except Exception as e:
        result["errors"].append(f"sandbox: {e}")
        log.error("[backup] sandbox snapshot failed: %s", e)

    return result

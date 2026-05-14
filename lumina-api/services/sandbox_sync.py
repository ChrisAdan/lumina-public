"""
Bidirectional vault ↔ sandbox sync.

The sandbox (LUMINA_SANDBOX_DIR, /app/sandbox) is a bind-mount that mirrors
the Obsidian vault (LUMINA_OBSIDIAN_VAULT_PATH). Lumina writes to the sandbox;
human edits happen in the vault. Every 5 minutes the scheduler calls sync_once()
to push diffs between the two mirrors.

Conflict rule: human edit (vault) wins. When both sides modified a file since
the last sync, the vault version overwrites the sandbox copy.

Upsert-only: files are never deleted by this sync. Deletions must be manual.

State: a .last_sync sentinel file in SANDBOX_ROOT stores the last successful
sync timestamp as a float Unix epoch string.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from configs.app import LUMINA_OBSIDIAN_VAULT_PATH

log = logging.getLogger("lumina.sandbox_sync")

SANDBOX_DIR = Path(os.environ.get("LUMINA_SANDBOX_DIR", "/app/sandbox")).resolve()
VAULT_DIR   = Path(LUMINA_OBSIDIAN_VAULT_PATH).expanduser().resolve()
_SENTINEL   = ".last_sync"


def _last_sync_ts() -> float:
    sentinel = SANDBOX_DIR / _SENTINEL
    if sentinel.exists():
        try:
            return float(sentinel.read_text().strip())
        except Exception:
            pass
    return 0.0


def _save_sync_ts(ts: float) -> None:
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    (SANDBOX_DIR / _SENTINEL).write_text(str(ts))


def sync_once() -> dict:
    """Run one bidirectional sync pass. Returns a summary dict."""
    if not VAULT_DIR.exists():
        log.warning(f"[SYNC] vault not found at {VAULT_DIR} — skipping")
        return {"skipped": True, "reason": f"vault not found: {VAULT_DIR}"}

    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    last_ts = _last_sync_ts()
    now     = time.time()

    vault_to_sandbox = 0
    sandbox_to_vault = 0
    conflicts        = 0
    errors           = 0

    # Pass 1 — vault → sandbox (vault wins on conflict)
    for vault_file in VAULT_DIR.rglob("*"):
        if not vault_file.is_file():
            continue
        rel           = vault_file.relative_to(VAULT_DIR)
        sandbox_file  = SANDBOX_DIR / rel
        v_mtime       = vault_file.stat().st_mtime
        s_mtime       = sandbox_file.stat().st_mtime if sandbox_file.exists() else 0.0
        vault_changed  = v_mtime > last_ts
        sandbox_changed = s_mtime > last_ts

        if not vault_changed:
            continue  # vault side unchanged; sandbox→vault pass handles the other case

        if sandbox_changed:
            conflicts += 1
            log.info(f"[SYNC] conflict {rel} — vault wins (human edit priority)")

        try:
            sandbox_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vault_file, sandbox_file)
            vault_to_sandbox += 1
        except Exception as e:
            log.warning(f"[SYNC] vault→sandbox failed for {rel}: {e}")
            errors += 1

    # Pass 2 — sandbox → vault (only files vault side hasn't already overwritten)
    for sandbox_file in SANDBOX_DIR.rglob("*"):
        if not sandbox_file.is_file() or sandbox_file.name == _SENTINEL:
            continue
        rel         = sandbox_file.relative_to(SANDBOX_DIR)
        vault_file  = VAULT_DIR / rel
        s_mtime     = sandbox_file.stat().st_mtime
        v_mtime     = vault_file.stat().st_mtime if vault_file.exists() else 0.0

        if s_mtime <= last_ts:
            continue  # sandbox file hasn't changed since last sync
        if v_mtime > last_ts:
            continue  # vault side changed → already handled in pass 1 (vault wins)

        try:
            vault_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sandbox_file, vault_file)
            sandbox_to_vault += 1
        except Exception as e:
            log.warning(f"[SYNC] sandbox→vault failed for {rel}: {e}")
            errors += 1

    _save_sync_ts(now)
    summary = {
        "vault_to_sandbox": vault_to_sandbox,
        "sandbox_to_vault": sandbox_to_vault,
        "conflicts": conflicts,
        "errors": errors,
    }
    log.info(
        f"[SYNC] vault↔sandbox done — "
        f"v→s:{vault_to_sandbox} s→v:{sandbox_to_vault} "
        f"conflicts:{conflicts} errors:{errors}"
    )
    return summary

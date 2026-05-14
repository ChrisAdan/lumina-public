"""
Obsidian vault sync — keep the library's md docs in sync with the vault on disk.

Run on a 15-minute APScheduler cron and on demand via POST /library/sync. Walk
the vault, sha256-dedupe each `.md` file against the existing documents row,
ingest changed/new files, delete orphans (rows whose source_path no longer
exists or whose path moved out of the vault).

Skips:
- Anything outside `LUMINA_OBSIDIAN_VAULT_PATH`
- `.obsidian/`, `.trash/`, and any other dotted directory
- Non-`.md` files
"""
from __future__ import annotations

import logging
from pathlib import Path

from configs.app import LUMINA_OBSIDIAN_VAULT_PATH
from repos import documents as documents_repo
from services.markdown_ingest import ingest_markdown
from services.pdf_ingest import _wipe_doc, file_sha256

logger = logging.getLogger("lumina.vault_sync")

SUPPORTED_EXTS = {".md"}


def _iter_notes(root: Path):
    """Yield every `.md` file under `root`, skipping dotted dirs (.obsidian,
    .trash) and non-markdown files. Sorted for deterministic logs."""
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        yield p


def sync_vault(vault_path: str | Path | None = None) -> dict:
    """Full sync pass. Returns {ingested, updated, skipped, deleted, errored}."""
    root = Path(vault_path or LUMINA_OBSIDIAN_VAULT_PATH).expanduser().resolve()
    if not root.exists():
        logger.warning("vault path missing, skipping sync: %s", root)
        return {"ingested": 0, "updated": 0, "skipped": 0, "deleted": 0, "errored": 0}

    seen_paths: set[str] = set()
    ingested = updated = skipped = errored = 0

    for path in _iter_notes(root):
        seen_paths.add(str(path))
        try:
            sha = file_sha256(path)
            # Match by source_path so a file rename creates a new doc (and the
            # old one becomes an orphan, deleted in the second pass below).
            from sqlalchemy import text
            from db.postgres import engine
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT id, sha256 FROM documents WHERE source_path = :p"),
                    {"p": str(path)},
                ).mappings().first()

            if row and row["sha256"] == sha:
                skipped += 1
                continue

            ingest_markdown(path=path, vault_root=root)
            if row:
                updated += 1
                logger.info("[vault_sync] updated %s", path.relative_to(root))
            else:
                ingested += 1
                logger.info("[vault_sync] ingested %s", path.relative_to(root))
        except Exception as exc:
            errored += 1
            logger.error("[vault_sync] failed %s: %s", path, exc)

    # Orphan pass: any md doc whose source_path is no longer in the vault.
    deleted = 0
    for doc in documents_repo.list_all():
        if (doc.metadata or {}).get("format") != "md":
            continue
        if doc.source_path in seen_paths:
            continue
        if not Path(doc.source_path).exists() or not doc.source_path.startswith(str(root)):
            try:
                _wipe_doc(doc.id)
                documents_repo.delete(doc.id)
                deleted += 1
                logger.info("[vault_sync] deleted orphan %s", doc.id)
            except Exception as exc:
                errored += 1
                logger.error("[vault_sync] orphan delete failed %s: %s", doc.id, exc)

    summary = {
        "ingested": ingested,
        "updated": updated,
        "skipped": skipped,
        "deleted": deleted,
        "errored": errored,
        "vault_path": str(root),
    }
    logger.info("[vault_sync] %s", summary)
    return summary

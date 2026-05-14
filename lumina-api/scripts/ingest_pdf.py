#!/usr/bin/env python3
"""
Ingest documents (PDF / EPUB / EPUB3) into the library.

Two modes, dispatched by whether --path points at a file or a directory:

  Single file (legacy)
      docker compose exec -e PYTHONPATH=/app lumina-api \\
        python scripts/ingest_pdf.py \\
          --path library/some-manual.pdf \\
          --doc-id some_manual_v1 \\
          --title "Some Manual"

  Directory scan (idempotent)
      docker compose exec -e PYTHONPATH=/app lumina-api \\
        python scripts/ingest_pdf.py --path library/

      Walks the directory recursively, skips files whose sha256 already
      matches an ingested document. doc_id and title are auto-derived from
      each filename. Use --force to re-ingest everything.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SUPPORTED_EXTS = {".pdf", ".epub", ".epub3"}


def _slugify(stem: str) -> str:
    """Filename stem → doc_id. Lowercase, non-alphanumeric → '_', collapsed."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", stem).strip("_").lower()
    return slug or "doc"


def _pretty_title(stem: str) -> str:
    """Filename stem → title. Just normalize separators; don't get fancy."""
    return re.sub(r"[_\-]+", " ", stem).strip()


def _ingest_one(path: Path, doc_id: str, title: str) -> dict:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from services.pdf_ingest import ingest_pdf
        return ingest_pdf(path=str(path), doc_id=doc_id, title=title)
    if suffix in (".epub", ".epub3"):
        from services.epub_ingest import ingest_epub
        return ingest_epub(path=str(path), doc_id=doc_id, title=title)
    raise ValueError(f"unsupported extension {suffix!r} for {path}")


def _iter_supported(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            yield p


def _scan_dir(root: Path, force: bool) -> int:
    from repos import documents as documents_repo
    from services.pdf_ingest import file_sha256

    ingested = skipped = errored = 0
    for path in _iter_supported(root):
        rel = path.relative_to(root)
        try:
            sha = file_sha256(path)
            existing = documents_repo.get_by_sha256(sha)
            if existing and not force:
                print(f"[skip] {rel} → {existing.id} (sha256 matches)")
                skipped += 1
                continue

            stem = path.stem
            doc_id = _slugify(stem)
            title = _pretty_title(stem)
            summary = _ingest_one(path, doc_id=doc_id, title=title)
            unit = "chapters" if summary.get("format") == "epub" else "pages"
            unit_count = summary.get("chapter_count", summary.get("page_count", "?"))
            print(
                f"[ingested] {rel} → {doc_id}: "
                f"{unit_count} {unit}, {summary['chunk_count']} chunks"
            )
            ingested += 1
        except Exception as exc:
            print(f"[error] {rel}: {type(exc).__name__}: {exc}", file=sys.stderr)
            errored += 1

    print(f"---\n{ingested} ingested, {skipped} skipped, {errored} errored.")
    return 0 if errored == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest documents (PDF / EPUB) into the library.")
    parser.add_argument("--path", required=True, help="file OR directory (relative to /app inside the container)")
    parser.add_argument("--doc-id", help="single-file mode: stable slug, e.g. supernote_nomad_v1")
    parser.add_argument("--title", help="single-file mode: human-readable title")
    parser.add_argument("--force", action="store_true", help="dir-scan mode: re-ingest even if sha256 matches an existing doc")
    args = parser.parse_args()

    target = Path(args.path)
    if not target.exists():
        print(f"path not found: {target}", file=sys.stderr)
        return 2

    if target.is_dir():
        if args.doc_id or args.title:
            print("--doc-id / --title cannot be combined with a directory path", file=sys.stderr)
            return 2
        return _scan_dir(target, force=args.force)

    # Single-file mode (legacy).
    if not args.doc_id or not args.title:
        print("--doc-id and --title are required when --path is a file", file=sys.stderr)
        return 2
    if target.suffix.lower() not in SUPPORTED_EXTS:
        print(f"unsupported file extension: {target.suffix!r}; expected one of {sorted(SUPPORTED_EXTS)}", file=sys.stderr)
        return 2
    summary = _ingest_one(target, doc_id=args.doc_id, title=args.title)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

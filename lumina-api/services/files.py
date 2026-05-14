"""
File I/O for Lumina.

file_read  — reads any file on the container filesystem by absolute path, or
             any file in the vault sandbox by relative path. No write surface.

file_write — APPEND-ONLY to existing files; creates new files in full.
             Target root: LUMINA_SANDBOX_DIR (/app/sandbox) — a bind-mount
             that mirrors the Obsidian vault. Human edits always win on conflict;
             the bidirectional sync (sandbox_sync.py, every 5 min) pushes diffs
             between the sandbox and the live vault.

file_list  — lists all files under LUMINA_SANDBOX_DIR (the vault mirror).
             Pass `subdir` to scope the listing; omit for the full tree.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

SANDBOX_ROOT = Path(os.environ.get("LUMINA_SANDBOX_DIR", "/app/sandbox")).resolve()
PROJECT_ROOT = Path("/app").resolve()
MAX_READ_CHARS   = 100_000   # generous for source files, vault notes, configs
MAX_APPEND_BYTES = 500_000   # 500 KB per append / create


def _ensure_sandbox() -> None:
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)


def _resolve_read_path(path: str) -> Path:
    """Accept absolute paths (starts with /) or sandbox-relative paths."""
    if not path or not path.strip():
        raise ValueError("path is required")
    p = Path(path)
    if p.is_absolute():
        return p.resolve()
    return (SANDBOX_ROOT / path).resolve()


def _resolve_write_path(path: str) -> Path:
    """Write paths must stay within SANDBOX_ROOT."""
    if not path or not path.strip():
        raise ValueError("path is required")
    candidate = (SANDBOX_ROOT / path).resolve()
    if not candidate.is_relative_to(SANDBOX_ROOT):
        raise ValueError(f"path escapes sandbox: {path}")
    return candidate


async def file_read(path: str) -> dict[str, Any]:
    try:
        p = _resolve_read_path(path)
    except ValueError as e:
        return {"error": str(e)}
    if not p.exists():
        return {"error": f"not found: {path}"}
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return {"error": f"permission denied: {path}"}
    except Exception as e:
        return {"error": f"read error: {e}"}
    truncated = len(text) > MAX_READ_CHARS
    return {
        "path": str(p),
        "size_bytes": p.stat().st_size,
        "content": text[:MAX_READ_CHARS] if truncated else text,
        "truncated": truncated,
    }


async def file_write(path: str, content: str) -> dict[str, Any]:
    """Append to existing files; create new files in full. Sandbox-scoped."""
    _ensure_sandbox()
    try:
        p = _resolve_write_path(path)
    except ValueError as e:
        return {"error": str(e)}
    data = (content or "").encode("utf-8")
    if len(data) > MAX_APPEND_BYTES:
        return {"error": f"content exceeds {MAX_APPEND_BYTES // 1000}KB cap ({len(data)} bytes)"}
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.is_file()
    if existed:
        # Append-only: ensure a clean newline separator before the new content
        last_byte = b""
        if p.stat().st_size > 0:
            with p.open("rb") as rf:
                rf.seek(-1, 2)
                last_byte = rf.read(1)
        with p.open("ab") as f:
            if last_byte and last_byte != b"\n":
                f.write(b"\n")
            f.write(data)
        return {
            "path": str(p.relative_to(SANDBOX_ROOT)),
            "bytes_appended": len(data),
            "mode": "append",
            "note": "content appended to end of existing file; sync pushes to vault within 5 min",
        }
    else:
        p.write_bytes(data)
        return {
            "path": str(p.relative_to(SANDBOX_ROOT)),
            "bytes_written": len(data),
            "mode": "create",
            "note": "new file created; sync pushes to vault within 5 min",
        }


_GREP_SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".pdf", ".epub", ".zip", ".tar", ".gz", ".bin", ".so", ".whl",
    ".db", ".sqlite", ".lock",
}
_GREP_MAX_FILE_SIZE = 300_000   # skip files larger than this (bytes)
_GREP_MAX_FILES     = 200       # stop scanning after this many files searched


def _resolve_list_path(subdir: str) -> tuple[Path, Path]:
    """Return (base, display_root) for file_list.

    Accepts:
      - absolute path under /app  → lists that directory, paths displayed relative to /app
      - sandbox-relative path     → lists within SANDBOX_ROOT, paths relative to SANDBOX_ROOT
    """
    p = Path(subdir)
    if p.is_absolute():
        resolved = p.resolve()
        if not resolved.is_relative_to(PROJECT_ROOT):
            raise ValueError(f"absolute path must be under {PROJECT_ROOT}: {subdir}")
        return resolved, PROJECT_ROOT
    candidate = (SANDBOX_ROOT / subdir).resolve()
    if not candidate.is_relative_to(SANDBOX_ROOT):
        raise ValueError(f"path escapes sandbox: {subdir}")
    return candidate, SANDBOX_ROOT


async def file_list(subdir: str = "") -> dict[str, Any]:
    """List files in the vault sandbox or the project source tree (/app)."""
    _ensure_sandbox()
    if subdir.strip():
        try:
            base, display_root = _resolve_list_path(subdir)
        except ValueError as e:
            return {"error": str(e)}
    else:
        base, display_root = SANDBOX_ROOT, SANDBOX_ROOT

    if not base.exists():
        return {"root": str(display_root), "count": 0, "files": []}
    if base.is_file():
        return {"error": f"{subdir!r} is a file, not a directory"}
    files = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        # skip compiled / cache files when browsing source
        if p.suffix in {".pyc", ".pyo"} or "__pycache__" in p.parts:
            continue
        files.append({
            "path": str(p.relative_to(display_root)),
            "size_bytes": p.stat().st_size,
        })

    # Guard: when listing source directories, a flat recursive walk can return
    # hundreds of files and overflow the model's context. Cap at 80 entries and
    # suggest narrowing the path.
    MAX_FILES = 80
    if len(files) > MAX_FILES and base != SANDBOX_ROOT:
        subdirs = sorted({
            str(p.relative_to(display_root)).split("/")[0]
            for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")
        })
        return {
            "root": str(display_root),
            "count": len(files),
            "truncated": True,
            "note": f"Too many files ({len(files)}) to list at once. Call file_list with a subdirectory path to narrow the scope.",
            "top_level_dirs": subdirs,
        }

    return {"root": str(display_root), "count": len(files), "files": files}


async def file_grep(
    pattern: str,
    path: str = "",
    glob: str = "**/*",
    context_lines: int = 2,
    max_matches: int = 30,
    is_regex: bool = False,
) -> dict[str, Any]:
    """Search file contents for a pattern. Works on vault (sandbox-relative)
    and Lumina source (/app absolute paths)."""
    import re as _re

    _ensure_sandbox()
    if path.strip():
        try:
            base, display_root = _resolve_list_path(path)
        except ValueError as e:
            return {"error": str(e)}
    else:
        base, display_root = SANDBOX_ROOT, SANDBOX_ROOT

    if not base.exists():
        return {"error": f"path not found: {path or '(sandbox root)'}"}

    try:
        rx = _re.compile(pattern, _re.IGNORECASE) if is_regex else None
    except _re.error as e:
        return {"error": f"invalid regex: {e}"}

    matches: list[dict[str, Any]] = []
    files_searched = 0
    truncated = False

    for p in sorted(base.glob(glob)):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix in _GREP_SKIP_SUFFIXES or "__pycache__" in p.parts:
            continue
        if p.stat().st_size > _GREP_MAX_FILE_SIZE:
            continue
        if files_searched >= _GREP_MAX_FILES:
            truncated = True
            break

        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        files_searched += 1

        for lineno, line in enumerate(lines, start=1):
            hit = (rx.search(line) is not None) if rx else (pattern.lower() in line.lower())
            if not hit:
                continue
            ctx_start = max(0, lineno - 1 - context_lines)
            ctx_end   = min(len(lines), lineno + context_lines)
            matches.append({
                "file": str(p.relative_to(display_root)),
                "line": lineno,
                "text": line,
                "context": lines[ctx_start:ctx_end],
            })
            if len(matches) >= max_matches:
                truncated = True
                break
        if truncated:
            break

    result: dict[str, Any] = {
        "pattern": pattern,
        "root": str(display_root),
        "files_searched": files_searched,
        "match_count": len(matches),
        "matches": matches,
    }
    if truncated:
        result["truncated"] = True
        result["note"] = "Hit limit — narrow path or glob to see more results."
    return result

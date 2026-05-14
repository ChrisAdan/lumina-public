"""
Parse the user's calendar sync list from
`{vault}/Lumina/Synapses/Calendars.md`.

Format (one calendar per line):

    Display Name | calendar_id

`calendar_id` is whatever Google's API expects:
  - an email address for personal calendars (e.g. `chris@gmail.com`)
  - a `*@group.calendar.google.com` ID for shared calendars

Lines that are blank, start with `#` (Markdown headings), or have no `|` are
skipped. Whitespace around the name and id is trimmed. Duplicate calendar_ids
are de-duplicated, keeping the first occurrence.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from configs.app import LUMINA_OBSIDIAN_VAULT_PATH

logger = logging.getLogger(__name__)

CALENDARS_MD_RELPATH = "Lumina/Synapses/Calendars.md"


@dataclass(frozen=True)
class CalendarTarget:
    name: str         # display name, e.g. "Chris"
    calendar_id: str  # Google Calendar API id


def _calendars_md_path() -> Path:
    return Path(LUMINA_OBSIDIAN_VAULT_PATH) / CALENDARS_MD_RELPATH


def parse_calendars_md(text: str) -> list[CalendarTarget]:
    out: list[CalendarTarget] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            continue
        name_part, _, id_part = line.partition("|")
        name = name_part.strip()
        cal_id = id_part.strip()
        if not name or not cal_id:
            continue
        if cal_id in seen:
            continue
        seen.add(cal_id)
        out.append(CalendarTarget(name=name, calendar_id=cal_id))
    return out


def load_calendar_targets() -> list[CalendarTarget]:
    """Read and parse the Calendars.md file from the Obsidian vault.

    Returns an empty list if the file is missing — callers should treat that as
    "nothing configured" rather than an error, so the CRON keeps running cleanly
    while the user is still setting things up.
    """
    path = _calendars_md_path()
    if not path.is_file():
        logger.warning("Calendars.md not found at %s — sync will be a no-op", path)
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    targets = parse_calendars_md(text)
    logger.info("Loaded %d calendar target(s) from %s", len(targets), path)
    return targets

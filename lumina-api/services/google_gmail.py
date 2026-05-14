"""
services/google_gmail.py

Fetch recent Gmail messages, deduplicate against email_index in Postgres
(via repos.gmail), strip HTML, and embed into ChromaDB 'emails' collection.

Called by:
  - APScheduler CRON (7AM UTC daily)
  - POST /gmail/sync (manual trigger)
"""
import base64
import logging
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup
from googleapiclient.discovery import build

from configs.app import GMAIL_EXCLUDED_LABELS, GMAIL_LOOKBACK_DAYS
from db.chroma import get_chroma_client
from repos import gmail as gmail_repo
from schemas.gmail import EmailIndexUpsert
from services.google_auth import get_credentials

logger = logging.getLogger(__name__)

CHROMA_COLLECTION = "emails"
BODY_TRUNCATE_CHARS = 1500


# ── Text extraction ──────────────────────────────────────────────────────────

def _decode_part(part: dict) -> str | None:
    data = part.get("body", {}).get("data")
    if not data:
        return None
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_body(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_part(payload) or ""
    if mime == "text/html":
        raw = _decode_part(payload) or ""
        return BeautifulSoup(raw, "lxml").get_text(separator=" ", strip=True)

    parts = payload.get("parts", [])
    plain = ""
    html_fallback = ""
    for part in parts:
        sub_mime = part.get("mimeType", "")
        if sub_mime == "text/plain":
            plain = _decode_part(part) or ""
        elif sub_mime == "text/html" and not plain:
            raw = _decode_part(part) or ""
            html_fallback = BeautifulSoup(raw, "lxml").get_text(separator=" ", strip=True)
        elif sub_mime.startswith("multipart/"):
            nested = _extract_body(part)
            if nested:
                plain = nested
                break
    return plain or html_fallback


def _parse_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_received_at(headers: list[dict]) -> datetime:
    date_str = _parse_header(headers, "Date")
    if date_str:
        try:
            return parsedate_to_datetime(date_str).astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


# ── Gmail API ────────────────────────────────────────────────────────────────

def _build_service():
    return build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)


def _list_message_ids(service, lookback_days: int) -> list[str]:
    after_ts = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())
    query = f"after:{after_ts}"
    ids: list[str] = []
    page_token = None
    while True:
        kwargs: dict[str, Any] = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        for m in result.get("messages", []):
            ids.append(m["id"])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    logger.info("Gmail: found %d message IDs in last %d days", len(ids), lookback_days)
    return ids


def _fetch_message(service, message_id: str) -> dict | None:
    try:
        return service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except Exception as exc:
        logger.warning("Failed to fetch message %s: %s", message_id, exc)
        return None


# ── Chroma ───────────────────────────────────────────────────────────────────

def _build_chroma_document(subject: str, sender: str, body: str) -> str:
    return f"Subject: {subject}\nFrom: {sender}\n\n{body[:BODY_TRUNCATE_CHARS].strip()}"


# ── Core sync ────────────────────────────────────────────────────────────────

def sync_gmail() -> dict[str, Any]:
    """
    1. List message IDs for last GMAIL_LOOKBACK_DAYS
    2. Diff against email_index via repo (skip already embedded)
    3. Fetch, parse, embed, then upsert into email_index
    """
    logger.info("Gmail sync started (lookback=%d days)", GMAIL_LOOKBACK_DAYS)
    service = _build_service()
    all_ids = _list_message_ids(service, GMAIL_LOOKBACK_DAYS)

    already = gmail_repo.already_indexed(all_ids)
    new_ids = [mid for mid in all_ids if mid not in already]
    logger.info("Gmail: %d new messages to embed (skipping %d already indexed)",
                len(new_ids), len(already))

    if not new_ids:
        return {"fetched": len(all_ids), "new": 0, "embedded": 0}

    collection = get_chroma_client().get_or_create_collection(CHROMA_COLLECTION)
    embedded = 0

    for message_id in new_ids:
        raw = _fetch_message(service, message_id)
        if not raw:
            continue

        if any(lbl in raw.get("labelIds", []) for lbl in GMAIL_EXCLUDED_LABELS):
            logger.debug("Skipping message %s — excluded label", message_id)
            continue

        headers = raw.get("payload", {}).get("headers", [])
        subject = _parse_header(headers, "Subject") or "(no subject)"
        sender = _parse_header(headers, "From") or "(unknown sender)"
        received_at = _parse_received_at(headers)
        snippet = raw.get("snippet", "")
        thread_id = raw.get("threadId", "")
        body = _extract_body(raw.get("payload", {}))

        try:
            collection.upsert(
                ids=[message_id],
                documents=[_build_chroma_document(subject, sender, body)],
                metadatas=[{
                    "message_id": message_id,
                    "sender": sender,
                    "subject": subject,
                    "received_at": received_at.isoformat(),
                }],
            )
            gmail_repo.insert(EmailIndexUpsert(
                message_id=message_id,
                thread_id=thread_id,
                subject=subject,
                sender=sender,
                received_at=received_at,
                snippet=snippet,
            ))
            embedded += 1
        except Exception as exc:
            logger.error("Failed to embed message %s: %s", message_id, exc)

    summary = {"fetched": len(all_ids), "new": len(new_ids), "embedded": embedded}
    logger.info("Gmail sync complete: %s", summary)
    return summary


# ── Send ─────────────────────────────────────────────────────────────────────

def send_email(to: str, subject: str, body: str, cc: str | None = None) -> dict[str, Any]:
    """Send a plain-text email via the authenticated Gmail account.

    Returns the sent message ID on success.
    Raises on API error — callers should catch and return a user-facing error.
    """
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    msg.attach(MIMEText(body, "plain", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service = _build_service()
    sent = service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()
    message_id = sent.get("id", "")
    logger.info("Gmail send: message_id=%s to=%s subject=%r", message_id, to, subject)
    return {"message_id": message_id, "to": to, "subject": subject}

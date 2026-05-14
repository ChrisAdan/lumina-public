"""APScheduler job definitions and registration for Lumina.

Active jobs: weather_refresh, sandbox_sync, vault_sync, people_sync,
             calendar_sync, gmail_sync, weekly_backup, reminder_check.

Commented-out jobs (reinstate when you add the matching router):
  - job_plaid_sync  → expenses router + Plaid creds
  - job_drive_sync  → Drive router
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import httpx

from configs.app import LOCAL_TIMEZONE, LUMINA_PEOPLE_SYNC_INTERVAL_MINUTES
from services.sandbox_sync import sync_once as _sandbox_sync_once

logger = logging.getLogger("scheduler")
scheduler = AsyncIOScheduler()

_STARTUP_DELAY_S = 15
_BASE_URL = "http://localhost:8000"


def _after_startup(seconds: int = _STARTUP_DELAY_S) -> datetime:
    return datetime.now() + timedelta(seconds=seconds)


async def _http_post_sync(endpoint: str, timeout: float, name: str, log_keys: list[str]) -> None:
    """POST to a local sync endpoint and log the result. Shared by all HTTP-based cron jobs."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{_BASE_URL}{endpoint}")
            resp.raise_for_status()
            data = resp.json()
            parts = " ".join(f"{k}:{data.get(k)}" for k in log_keys)
            logger.info("[CRON] %s — %s", name, parts)
    except Exception as e:
        logger.error("[CRON] %s failed: %s", name, e)


# ============================================================
# JOBS
# ============================================================

async def job_weather_refresh():
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{_BASE_URL}/weather/refresh")
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                "[CRON] Weather refreshed — %s Hi:%s°F Lo:%s°F",
                data.get("today_condition"),
                data.get("today_high_f"),
                data.get("today_low_f"),
            )
    except Exception as e:
        logger.error("[CRON] Weather refresh failed: %s", e)


async def job_sandbox_sync():
    """5-min bidirectional vault ↔ sandbox sync. Runs in-process (no HTTP round-trip)."""
    try:
        result = _sandbox_sync_once()
        if result.get("skipped"):
            logger.warning("[CRON] Sandbox sync skipped: %s", result.get("reason"))
        else:
            logger.info(
                "[CRON] Sandbox sync — v→s:%s s→v:%s conflicts:%s errors:%s",
                result.get("vault_to_sandbox"), result.get("sandbox_to_vault"),
                result.get("conflicts"), result.get("errors"),
            )
    except Exception as e:
        logger.error("[CRON] Sandbox sync failed: %s", e)


async def job_vault_sync():
    await _http_post_sync(
        "/library/sync", 120.0, "Vault sync",
        ["ingested", "updated", "skipped", "deleted", "errored"],
    )


async def job_people_sync():
    await _http_post_sync(
        "/people/sync", 120.0, "People sync",
        ["ingested", "updated", "skipped", "deleted", "errored", "people_count"],
    )


async def job_calendar_sync():
    await _http_post_sync(
        "/calendar/sync", 120.0, "Calendar sync",
        ["calendars", "fetched", "upserted", "embedded", "errored"],
    )


async def job_gmail_sync():
    await _http_post_sync(
        "/gmail/sync", 120.0, "Gmail sync",
        ["fetched", "new", "embedded"],
    )


# async def job_plaid_sync():
#     await _http_post_sync("/expenses/sync-all", 60.0, "Plaid sync", ["synced"])

# async def job_drive_sync():
#     await _http_post_sync("/drive/sync", 120.0, "Drive sync", ["scanned", "new", "updated", "embedded"])


async def job_weekly_backup():
    from services.backup import run_backup
    try:
        result = await run_backup()
        errors = result.get("errors", [])
        pg = result.get("postgres", {})
        sb = result.get("sandbox", {})
        if errors:
            logger.error("[CRON] Weekly backup partial — errors: %s", errors)
        else:
            logger.info(
                "[CRON] Weekly backup — db:%s (%sKB gz) sandbox:%s (%sKB gz)",
                pg.get("file"), pg.get("gz_bytes", 0) // 1024,
                sb.get("file"), sb.get("gz_bytes", 0) // 1024,
            )
    except Exception as e:
        logger.error("[CRON] Weekly backup failed: %s", e)


async def job_reminder_check():
    """Every-minute scan for due one-shot reminders — fire via ntfy, mark done."""
    try:
        from repos.reminders import get_due_oneshots, mark_fired
        from services.ntfy import send as ntfy_send
        due = get_due_oneshots()
        for r in due:
            await ntfy_send(r["message"], title="Lumina Reminder", topic=r["topic"])
            mark_fired(r["id"])
            logger.info("[CRON] Reminder fired — id=%s: %s", r["id"], r["message"][:60])
    except Exception as e:
        logger.error("[CRON] reminder_check failed: %s", e)


async def _fire_recurring_reminder(reminder_id: int, message: str, topic: str):
    """Called by APScheduler for each recurring cron job."""
    try:
        from repos.reminders import mark_fired
        from services.ntfy import send as ntfy_send
        await ntfy_send(message, title="Lumina Reminder", topic=topic)
        mark_fired(reminder_id)
        logger.info("[CRON] Recurring reminder fired — id=%s: %s", reminder_id, message[:60])
    except Exception as e:
        logger.error("[CRON] Recurring reminder id=%s failed: %s", reminder_id, e)


def schedule_reminder(reminder: dict) -> None:
    """Add a new reminder to the live scheduler. Called after tool writes to DB."""
    if not reminder.get("repeat") or not reminder.get("cron_expr"):
        return  # one-shots are handled by job_reminder_check
    _add_recurring_job(reminder)


def cancel_reminder_job(reminder_id: int) -> None:
    job_id = f"reminder_{reminder_id}"
    try:
        scheduler.remove_job(job_id)
        logger.info("[CRON] Cancelled reminder job %s", job_id)
    except Exception:
        pass  # job may not exist (one-shot or already removed)


def _add_recurring_job(reminder: dict) -> None:
    try:
        scheduler.add_job(
            _fire_recurring_reminder,
            trigger=CronTrigger.from_crontab(reminder["cron_expr"], timezone=LOCAL_TIMEZONE),
            id=f"reminder_{reminder['id']}",
            replace_existing=True,
            kwargs={
                "reminder_id": reminder["id"],
                "message": reminder["message"],
                "topic": reminder["topic"],
            },
        )
        logger.info(
            "[CRON] Scheduled recurring reminder id=%s cron=%s",
            reminder["id"], reminder["cron_expr"],
        )
    except Exception as e:
        logger.error("[CRON] Failed to schedule recurring reminder id=%s: %s", reminder["id"], e)


def _load_recurring_reminders() -> None:
    """On startup, reload all active recurring reminders from DB into APScheduler."""
    try:
        from repos.reminders import list_all_recurring
        for r in list_all_recurring():
            _add_recurring_job(r)
        logger.info("[CRON] Loaded recurring reminders from DB")
    except Exception as e:
        logger.warning("[CRON] Could not load recurring reminders (DB may not be ready): %s", e)


# ============================================================
# SCHEDULE REGISTRATION
# ============================================================

def start_scheduler():

    scheduler.add_job(
        job_weather_refresh,
        trigger=CronTrigger(hour=0, minute=0, timezone="UTC"),
        id="weather_refresh",
        replace_existing=True,
    )

    scheduler.add_job(
        job_sandbox_sync,
        trigger=IntervalTrigger(minutes=5, start_date=_after_startup(seconds=10)),
        id="sandbox_sync",
        replace_existing=True,
    )

    scheduler.add_job(
        job_vault_sync,
        trigger=IntervalTrigger(minutes=15, start_date=_after_startup()),
        id="vault_sync",
        replace_existing=True,
    )

    scheduler.add_job(
        job_people_sync,
        trigger=IntervalTrigger(
            minutes=max(1, LUMINA_PEOPLE_SYNC_INTERVAL_MINUTES),
            start_date=_after_startup(),
        ),
        id="people_sync",
        replace_existing=True,
    )

    scheduler.add_job(
        job_calendar_sync,
        trigger=IntervalTrigger(hours=1, start_date=_after_startup()),
        id="calendar_sync",
        replace_existing=True,
    )

    scheduler.add_job(
        job_gmail_sync,
        trigger=CronTrigger(hour=7, minute=0, timezone="UTC"),
        id="gmail_sync",
        replace_existing=True,
    )

    scheduler.add_job(
        job_weekly_backup,
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=0, timezone="UTC"),
        id="weekly_backup",
        replace_existing=True,
    )

    scheduler.add_job(
        job_reminder_check,
        trigger=IntervalTrigger(minutes=1, start_date=_after_startup(seconds=5)),
        id="reminder_check",
        replace_existing=True,
    )

    scheduler.start()
    _load_recurring_reminders()

    logger.info(
        "[CRON] Scheduler started — active jobs: weather_refresh, sandbox_sync, vault_sync, "
        "people_sync, calendar_sync, gmail_sync, weekly_backup, reminder_check"
    )


def stop_scheduler():
    scheduler.shutdown()
    logger.info("[CRON] Scheduler stopped")

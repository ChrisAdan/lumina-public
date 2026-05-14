"""
routers/briefing.py
GET /briefing — assembles a compact daily context payload for Ollama.

Designed to be called once at conversation start, replacing ~5 separate
tool calls. Every section is trimmed to be LLM-friendly: no raw JSONB
blobs, no full recipe text, just the facts the model needs to reason.

Response shape:
{
    "generated_at": "ISO datetime",
    "weather": { today summary },
    "groceries": { pending item count + list },
    "plants": { plants due for feeding today/overdue },
    "expenses": { last 7 days spend by category },
    "fitness": { active goals + last session summary },
    "open_trips": [ upcoming trips ],
}
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from db.postgres import get_db

router = APIRouter(prefix="/briefing", tags=["briefing"])


@router.get("/")
def daily_briefing(db: Session = Depends(get_db)):
    """
    Single-call daily context for Ollama. Replaces multi-tool fan-out at
    conversation start. All data is pre-trimmed — no raw blobs passed through.
    """
    today = date.today()
    seven_days_ago = today - timedelta(days=7)

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "weather":   _weather(db),
        "markets":   _markets(db),
        "groceries": _groceries(db),
        "plants":    _plants(db, today),
        "expenses":  _expenses(db, seven_days_ago),
        "fitness":   _fitness(db),
        "trips":     _trips(db, today),
    }


# ── Section builders ──────────────────────────────────────────────────────────

def _weather(db: Session) -> dict:
    row = db.execute(
        text("""
            SELECT location_name, forecast_date, today
            FROM weather_forecasts
            ORDER BY fetched_at DESC
            LIMIT 1
        """)
    ).fetchone()

    if not row or not row.today:
        return {"available": False}

    t = row.today  # JSONB dict
    return {
        "available":       True,
        "location":        row.location_name,
        "date":            str(row.forecast_date),
        "condition":       t.get("condition"),
        "temp_high_c":     t.get("temp_max_c"),
        "temp_low_c":      t.get("temp_min_c"),
        "precipitation_mm": t.get("precipitation_mm"),
        "wind_kph":        t.get("wind_kph"),
    }


def _markets(db: Session) -> dict:
    rows = db.execute(
        text("""
            SELECT DISTINCT ON (symbol)
                symbol, bar_date, open, high, low, close, volume, pct_change
            FROM market_daily
            ORDER BY symbol, bar_date DESC
        """)
    ).fetchall()
    if not rows:
        return {"available": False}
    return {
        "available": True,
        "as_of": str(rows[0].bar_date),
        "indices": [
            {
                "symbol":     r.symbol,
                "close":      float(r.close),
                "pct_change": float(r.pct_change) if r.pct_change is not None else None,
                "high":       float(r.high),
                "low":        float(r.low),
            }
            for r in rows
        ],
    }


def _groceries(db: Session) -> dict:
    rows = db.execute(
        text("""
            SELECT item, quantity, category
            FROM groceries
            WHERE completed = false
            ORDER BY category, item
        """)
    ).fetchall()

    return {
        "pending_count": len(rows),
        "items": [
            {"item": r.item, "quantity": r.quantity, "category": r.category}
            for r in rows
        ],
    }


def _plants(db: Session, today: date) -> dict:
    overdue = db.execute(
        text("""
            SELECT plant_type, variety, location, next_feed_date
            FROM plants
            WHERE is_active = true
              AND next_feed_date <= :today
            ORDER BY next_feed_date
        """),
        {"today": today},
    ).fetchall()

    upcoming = db.execute(
        text("""
            SELECT plant_type, variety, location, next_feed_date
            FROM plants
            WHERE is_active = true
              AND next_feed_date > :today
              AND next_feed_date <= :soon
            ORDER BY next_feed_date
        """),
        {"today": today, "soon": today + timedelta(days=3)},
    ).fetchall()

    def _fmt(r) -> dict:
        return {
            "plant": f"{r.variety or ''} {r.plant_type}".strip(),
            "location": r.location,
            "feed_date": str(r.next_feed_date),
        }

    return {
        "overdue":  [_fmt(r) for r in overdue],
        "due_soon": [_fmt(r) for r in upcoming],
    }


def _expenses(db: Session, since: date) -> dict:
    category_rows = db.execute(
        text("""
            SELECT category, SUM(amount) AS total, COUNT(*) AS txn_count
            FROM expenses
            WHERE transaction_date >= :since
              AND amount > 0
            GROUP BY category
            ORDER BY total DESC
        """),
        {"since": since},
    ).fetchall()

    total_spend = db.execute(
        text("""
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM expenses
            WHERE transaction_date >= :since AND amount > 0
        """),
        {"since": since},
    ).fetchone()

    return {
        "period_days": 7,
        "total_spend": float(total_spend.total) if total_spend else 0.0,
        "by_category": [
            {
                "category": r.category or "uncategorized",
                "total":     float(r.total),
                "transactions": r.txn_count,
            }
            for r in category_rows
        ],
    }


def _fitness(db: Session) -> dict:
    # Active strength goals
    goals = db.execute(
        text("""
            SELECT e.name AS exercise, sg.target_weight_kg, sg.target_reps, sg.target_date
            FROM strength_goals sg
            JOIN exercises e ON e.id = sg.exercise_id
            WHERE sg.achieved = false
            ORDER BY sg.target_date NULLS LAST
            LIMIT 5
        """)
    ).fetchall()

    # Most recent session
    last_session = db.execute(
        text("""
            SELECT ws.id, ws.started_at, ws.finished_at,
                   ws.perceived_effort, wp.name AS plan_name, ws.day_label
            FROM workout_sessions ws
            LEFT JOIN workout_plans wp ON wp.id = ws.plan_id
            ORDER BY ws.started_at DESC
            LIMIT 1
        """)
    ).fetchone()

    last_session_summary = None
    if last_session:
        set_count = db.execute(
            text("SELECT COUNT(*) AS n FROM session_sets WHERE session_id = :id"),
            {"id": last_session.id},
        ).fetchone()

        last_session_summary = {
            "date":             last_session.started_at.date().isoformat(),
            "plan":             last_session.plan_name,
            "day_label":        last_session.day_label,
            "sets_logged":      set_count.n if set_count else 0,
            "perceived_effort": last_session.perceived_effort,
        }

    return {
        "active_goals": [
            {
                "exercise":       r.exercise,
                "target_weight_kg": r.target_weight_kg,
                "target_reps":    r.target_reps,
                "target_date":    str(r.target_date) if r.target_date else None,
            }
            for r in goals
        ],
        "last_session": last_session_summary,
    }


def _trips(db: Session, today: date) -> dict:
    rows = db.execute(
        text("""
            SELECT destination, start_date, end_date, status
            FROM trips
            WHERE status IN ('planning', 'booked')
              AND (end_date IS NULL OR end_date >= :today)
            ORDER BY start_date NULLS LAST
            LIMIT 5
        """),
        {"today": today},
    ).fetchall()

    return {
        "upcoming": [
            {
                "destination": r.destination,
                "start_date":  str(r.start_date) if r.start_date else None,
                "end_date":    str(r.end_date) if r.end_date else None,
                "status":      r.status,
            }
            for r in rows
        ]
    }

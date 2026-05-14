import json
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from db.postgres import get_db
from services.weather import fetch_forecast
from configs.app import WEATHER_LOCATION_NAME, WEATHER_LATITUDE, WEATHER_LONGITUDE

router = APIRouter(prefix="/weather", tags=["weather"])


# ============================================================
# HELPERS
# ============================================================

def _latest_forecast(db: Session) -> dict | None:
    row = db.execute(
        text("""
            SELECT * FROM weather_forecasts
            ORDER BY fetched_at DESC
            LIMIT 1
        """)
    ).fetchone()
    if not row:
        return None
    d = dict(row._mapping)
    for field in ("today", "hourly", "daily"):
        if isinstance(d.get(field), str):
            try:
                d[field] = json.loads(d[field])
            except Exception:
                pass
    return d


def _store_forecast(db: Session, forecast: dict):
    query = """
        INSERT INTO weather_forecasts (
            location_name, latitude, longitude,
            fetched_at, forecast_date,
            today, hourly, daily
        ) VALUES (
            :location_name, :latitude, :longitude,
            :fetched_at, :forecast_date,
            :today::jsonb, :hourly::jsonb, :daily::jsonb
        )
        RETURNING *
    """
    row = db.execute(text(query), {
        "location_name": forecast["location_name"],
        "latitude": forecast["latitude"],
        "longitude": forecast["longitude"],
        "fetched_at": forecast["fetched_at"],
        "forecast_date": forecast["forecast_date"],
        "today": json.dumps(forecast["today"]),
        "hourly": json.dumps(forecast["hourly"]),
        "daily": json.dumps(forecast["daily"]),
    }).fetchone()
    db.commit()
    return dict(row._mapping)


# ============================================================
# ROUTES
# ============================================================

@router.get("/today")
def get_today(db: Session = Depends(get_db)):
    """
    Return today's forecast from DB.
    This is the primary endpoint for Lumina to hit when asked about the weather.
    Includes today's summary + hourly breakdown.
    """
    forecast = _latest_forecast(db)
    if not forecast:
        raise HTTPException(
            status_code=404,
            detail="No forecast data found. Trigger POST /weather/refresh to fetch."
        )
    return {
        "location": forecast["location_name"],
        "as_of": forecast["fetched_at"],
        "today": forecast["today"],
        "hourly": forecast["hourly"],
    }


@router.get("/forecast")
def get_forecast(db: Session = Depends(get_db)):
    """Return the full 7-day forecast from the latest DB record."""
    forecast = _latest_forecast(db)
    if not forecast:
        raise HTTPException(
            status_code=404,
            detail="No forecast data found. Trigger POST /weather/refresh to fetch."
        )
    return {
        "location": forecast["location_name"],
        "as_of": forecast["fetched_at"],
        "daily": forecast["daily"],
    }


@router.get("/history")
def get_forecast_history(limit: int = 7, db: Session = Depends(get_db)):
    """Return the last N stored forecast records — useful for observability."""
    rows = db.execute(
        text("""
            SELECT id, location_name, latitude, longitude, fetched_at, forecast_date
            FROM weather_forecasts
            ORDER BY fetched_at DESC
            LIMIT :limit
        """),
        {"limit": limit}
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/refresh", status_code=201)
async def refresh_forecast(db: Session = Depends(get_db)):
    """
    Manually trigger a fresh fetch from Open-Meteo and store in DB.
    This is also called by the CRON job at midnight UTC.
    Safe to call anytime — each call creates a new snapshot row.
    """
    try:
        forecast = await fetch_forecast()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Open-Meteo fetch failed: {str(e)}")

    stored = _store_forecast(db, forecast)
    return {
        "message": "Forecast refreshed successfully",
        "forecast_date": stored["forecast_date"],
        "fetched_at": stored["fetched_at"],
        "location": stored["location_name"],
        "today_condition": forecast["today"]["condition"] if forecast.get("today") else None,
        "today_high_f": forecast["today"]["temp_high_f"] if forecast.get("today") else None,
        "today_low_f": forecast["today"]["temp_low_f"] if forecast.get("today") else None,
    }

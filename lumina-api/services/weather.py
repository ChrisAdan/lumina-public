"""
services/weather.py
Handles fetching from Open-Meteo and transforming the response
into structured dicts ready for DB insertion or direct return.
"""
import httpx
from datetime import date, datetime, timezone as _tz
from typing import Optional

from configs.app import (
    OPEN_METEO_URL,
    WEATHER_LATITUDE,
    WEATHER_LONGITUDE,
    WEATHER_TIMEZONE,
    WEATHER_LOCATION_NAME,
)

# ============================================================
# WMO WEATHER CODE -> HUMAN READABLE
# https://open-meteo.com/en/docs#weathervariables
# ============================================================
WMO_CONDITIONS = {
    0:  "Clear sky",
    1:  "Mainly clear",
    2:  "Partly cloudy",
    3:  "Overcast",
    45: "Foggy",
    48: "Icy fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight showers",
    81: "Moderate showers",
    82: "Violent showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with heavy hail",
}


def _c_to_f(c: float) -> float:
    return round(c * 9 / 5 + 32, 1)


def _kmh_to_mph(kmh: float) -> float:
    return round(kmh * 0.621371, 1)


def _wmo_label(code: Optional[int]) -> Optional[str]:
    if code is None:
        return None
    return WMO_CONDITIONS.get(code, f"Unknown ({code})")


async def fetch_forecast(
    latitude: float = WEATHER_LATITUDE,
    longitude: float = WEATHER_LONGITUDE,
    timezone: str = WEATHER_TIMEZONE,
) -> dict:
    """
    Hit Open-Meteo and return a structured forecast dict with:
    - today: single DailySnapshot for today
    - hourly: list of HourlySnapshots for today (next 24h)
    - daily: 7-day DailySnapshot list
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone,
        "temperature_unit": "celsius",   # we convert to F ourselves
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
        "forecast_days": 7,
        "hourly": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "precipitation",
            "precipitation_probability",
            "wind_speed_10m",
            "weather_code",
        ]),
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "precipitation_probability_max",
            "wind_speed_10m_max",
            "uv_index_max",
            "weather_code",
            "sunrise",
            "sunset",
        ]),
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        raw = resp.json()

    today_str = date.today().isoformat()
    hourly = raw["hourly"]
    daily = raw["daily"]

    # ---- Build hourly snapshots (today only — first 24 entries) ----
    hourly_out = []
    for i, time_str in enumerate(hourly["time"]):
        if not time_str.startswith(today_str):
            continue
        hourly_out.append({
            "hour": time_str,
            "temp_f": _c_to_f(hourly["temperature_2m"][i]),
            "feels_like_f": _c_to_f(hourly["apparent_temperature"][i]),
            "precipitation_mm": hourly["precipitation"][i],
            "precipitation_prob": hourly["precipitation_probability"][i],
            "wind_speed_mph": _kmh_to_mph(hourly["wind_speed_10m"][i]),
            "weather_code": hourly["weather_code"][i],
            "condition": _wmo_label(hourly["weather_code"][i]),
        })

    # ---- Build daily snapshots (7 days) ----
    daily_out = []
    for i, day_str in enumerate(daily["time"]):
        daily_out.append({
            "date": day_str,
            "temp_high_f": _c_to_f(daily["temperature_2m_max"][i]),
            "temp_low_f": _c_to_f(daily["temperature_2m_min"][i]),
            "precipitation_sum_mm": daily["precipitation_sum"][i],
            "precipitation_prob_max": daily["precipitation_probability_max"][i],
            "wind_speed_max_mph": _kmh_to_mph(daily["wind_speed_10m_max"][i]),
            "uv_index_max": daily["uv_index_max"][i],
            "weather_code": daily["weather_code"][i],
            "condition": _wmo_label(daily["weather_code"][i]),
            "sunrise": daily["sunrise"][i],
            "sunset": daily["sunset"][i],
        })

    today_daily = next((d for d in daily_out if d["date"] == today_str), None)

    return {
        "location_name": WEATHER_LOCATION_NAME,
        "latitude": latitude,
        "longitude": longitude,
        "fetched_at": datetime.now(_tz.utc).isoformat(),
        "forecast_date": today_str,
        "today": today_daily,
        "hourly": hourly_out,
        "daily": daily_out,
    }

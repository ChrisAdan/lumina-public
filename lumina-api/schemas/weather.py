from pydantic import BaseModel
from datetime import date, datetime
from typing import Optional, List


class HourlySnapshot(BaseModel):
    hour: str                           # ISO datetime string e.g. "2026-04-17T14:00"
    temp_f: float
    feels_like_f: Optional[float] = None
    precipitation_mm: Optional[float] = None
    precipitation_prob: Optional[int] = None   # 0-100
    wind_speed_mph: Optional[float] = None
    weather_code: Optional[int] = None
    condition: Optional[str] = None            # human-readable, derived from WMO code


class DailySnapshot(BaseModel):
    date: date
    temp_high_f: float
    temp_low_f: float
    precipitation_sum_mm: Optional[float] = None
    precipitation_prob_max: Optional[int] = None
    wind_speed_max_mph: Optional[float] = None
    uv_index_max: Optional[float] = None
    weather_code: Optional[int] = None
    condition: Optional[str] = None
    sunrise: Optional[str] = None
    sunset: Optional[str] = None


class WeatherForecastOut(BaseModel):
    id: int
    location_name: str
    latitude: float
    longitude: float
    fetched_at: datetime
    forecast_date: date
    today: Optional[DailySnapshot] = None
    hourly: Optional[List[HourlySnapshot]] = None
    daily: Optional[List[DailySnapshot]] = None

    class Config:
        orm_mode = True

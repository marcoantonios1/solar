import pandas as pd
import time
from dataclasses import dataclass
from datetime import datetime

from config_loader import config
from load_model import get_expected_load
from weather import fetch_forecast_weather, parse_forecast_timestamp
from solar_model import get_weather_adjusted_expected_power

FORECAST_CACHE_SECONDS = config["weather_api"].get("forecast_cache_seconds", 900)
CAPACITY_KWH_USABLE = config["battery"]["capacity_kwh_usable"]

_solar_forecast_cache = {"data": None, "fetched_at": None}


@dataclass(frozen=True)
class ProviderResult:
    """
    A single data point from a provider, with provenance - so downstream
    pipeline stages can check freshness/reliability, not just trust a bare
    number. `failed=True` means the underlying data source didn't respond;
    value_kwh will be 0 in that case, but callers must check `failed`
    rather than assuming 0 means "genuinely zero, nothing available".
    """
    value_kwh: float
    source: str
    fetched_at: datetime
    failed: bool = False


def battery_state(conn):
    """
    Provider: current battery energy (kWh), from the most recent live
    reading. The simplest provider - single data source, single failure
    mode (no recent reading exists).
    """
    row = conn.execute(
        "SELECT battery_soc, timestamp FROM readings WHERE battery_soc IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()

    if row is None:
        return ProviderResult(value_kwh=0, source="no_reading_available", fetched_at=datetime.now(), failed=True)

    soc_pct, timestamp_str = row
    kwh = (soc_pct / 100) * CAPACITY_KWH_USABLE

    return ProviderResult(value_kwh=round(kwh, 2), source="live_reading", fetched_at=datetime.fromisoformat(timestamp_str))


def load_model(conn, day_hours, night_hours):
    """
    Provider: expected house energy consumption (kWh) for a given split of
    day/night hours. Wraps the existing (already cached) get_expected_load(),
    converting its day/night watt rates into a single kWh figure for
    whatever window the caller needs - a 7-day cycle (Layer 1), the
    remaining hours until sunset (Layer 2), or until next sunrise (relax).
    """
    load_estimate = get_expected_load(conn)

    day_kwh = (load_estimate["day_load_w"] * day_hours) / 1000
    night_kwh = (load_estimate["night_load_w"] * night_hours) / 1000
    total_kwh = day_kwh + night_kwh

    source = f"day:{load_estimate['day_source']},night:{load_estimate['night_source']}"

    return ProviderResult(
        value_kwh=round(total_kwh, 2),
        source=source,
        fetched_at=datetime.now(),
        failed=False  # get_expected_load always returns a usable estimate (falls back to config seasonal defaults), never fails outright
    )


def solar_forecast(start, end):
    """
    Provider: expected solar generation (kWh) between any two timestamps.
    Works for a single day's remaining hours (Layer 2, relax) or - called
    once per cycle by the caller - for Layer 1's 7-day sunrise-to-sunrise
    cycles. The cycle-bucketing logic lives in the CALLER, not here - this
    provider just sums whatever window it's given.
    """
    now = pd.Timestamp.now(tz="Asia/Beirut")

    days_needed = max(1, (end - now).days + 2)

    current_time = time.time()
    cache_is_fresh = (
        _solar_forecast_cache["data"] is not None
        and _solar_forecast_cache["fetched_at"] is not None
        and (current_time - _solar_forecast_cache["fetched_at"]) < FORECAST_CACHE_SECONDS
        and _solar_forecast_cache.get("days_fetched", 0) >= days_needed
    )

    if cache_is_fresh:
        forecast = _solar_forecast_cache["data"]
    else:
        forecast = fetch_forecast_weather(days=days_needed)
        if forecast is not None:
            _solar_forecast_cache["data"] = forecast
            _solar_forecast_cache["fetched_at"] = current_time
            _solar_forecast_cache["days_fetched"] = days_needed

    if forecast is None:
        return ProviderResult(value_kwh=0, source="fetch_failed", fetched_at=datetime.now(), failed=True)

    total_kwh = 0.0
    hours_counted = 0
    for i, time_str in enumerate(forecast["time"]):
        timestamp = parse_forecast_timestamp(time_str)
        if timestamp is None:
            continue
        if start <= timestamp <= end:
            ghi, dni, dhi, temp = forecast["ghi"][i], forecast["dni"][i], forecast["dhi"][i], forecast["temp"][i]
            if any(v is None for v in [ghi, dni, dhi, temp]):
                continue
            result = get_weather_adjusted_expected_power(ghi=ghi, dni=dni, dhi=dhi, ambient_temp_c=temp, timestamp=timestamp)
            total_kwh += result["expected_power_w"] / 1000
            hours_counted += 1

    return ProviderResult(
        value_kwh=round(total_kwh, 2),
        source=f"open-meteo-forecast (hours_counted={hours_counted})",
        fetched_at=datetime.now(),
        failed=False
    )
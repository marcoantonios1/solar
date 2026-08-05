import requests
import time
import pandas as pd

from config_loader import config

LATITUDE = config["location"]["latitude"]
LONGITUDE = config["location"]["longitude"]

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

MAX_RETRIES = config["weather_api"]["max_retries"]
RETRY_DELAY_SECONDS = config["weather_api"]["retry_delay_seconds"]


def fetch_current_weather():
    """
    Returns a dict with cloud_cover (%), solar radiation values (GHI/DNI/DHI),
    and ambient temperature (C), or None if all retries fail. Never raises -
    the main poll loop should keep running even if weather fails entirely.
    """
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": "cloud_cover,shortwave_radiation,direct_normal_irradiance,diffuse_radiation,temperature_2m",
        "timezone": "auto"
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(OPEN_METEO_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            current = data.get("current", {})
            return {
                "cloud_cover": current.get("cloud_cover"),
                "ghi": current.get("shortwave_radiation"),
                "dni": current.get("direct_normal_irradiance"),
                "dhi": current.get("diffuse_radiation"),
                "ambient_temp_c": current.get("temperature_2m"),
            }
        except Exception as e:
            print(f"Weather fetch attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    print("All weather fetch retries failed.")
    return None


def fetch_forecast_weather(days=7):
    """
    Returns hourly forecast data for the next `days` days: lists of timestamps,
    GHI, DNI, DHI, and ambient temperature. Returns None if all retries fail.
    """
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "shortwave_radiation,direct_normal_irradiance,diffuse_radiation,temperature_2m",
        "forecast_days": days,
        "timezone": "auto"
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(OPEN_METEO_URL, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            hourly = data.get("hourly", {})
            return {
                "time": hourly.get("time", []),
                "ghi": hourly.get("shortwave_radiation", []),
                "dni": hourly.get("direct_normal_irradiance", []),
                "dhi": hourly.get("diffuse_radiation", []),
                "temp": hourly.get("temperature_2m", []),
            }
        except Exception as e:
            print(f"Forecast fetch attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    print("All forecast fetch retries failed.")
    return None

def parse_forecast_timestamp(time_str, tz="Asia/Beirut"):
    """
    Safely parses a naive forecast timestamp string into a tz-aware
    Timestamp, handling DST transitions gracefully instead of raising -
    the skipped hour on spring-forward, or the repeated hour on fall-back,
    would otherwise crash the whole prediction run. Returns None if the
    hour genuinely can't be resolved, so callers can skip it.
    """
    try:
        naive = pd.Timestamp(time_str)
        return naive.tz_localize(tz, nonexistent="shift_forward", ambiguous=True)
    except Exception as e:
        print(f"Skipping unparseable forecast timestamp {time_str}: {e}")
        return None
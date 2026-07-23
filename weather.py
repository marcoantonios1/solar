import requests

from config_loader import config

LATITUDE = config["location"]["latitude"]
LONGITUDE = config["location"]["longitude"]

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_current_weather():
    """
    Returns a dict with cloud_cover (%), solar radiation values (GHI/DNI/DHI),
    and ambient temperature (C), or None on failure. Never raises - the main
    poll loop should keep running even if weather fails.
    """
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": "cloud_cover,shortwave_radiation,direct_normal_irradiance,diffuse_radiation,temperature_2m",
        "timezone": "auto"
    }
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
        print(f"Weather fetch error: {e}")
        return None

def fetch_forecast_weather(days=7):
    """
    Returns hourly forecast data for the next `days` days: lists of timestamps,
    GHI, DNI, DHI, and ambient temperature. Returns None on failure.
    """
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "shortwave_radiation,direct_normal_irradiance,diffuse_radiation,temperature_2m",
        "forecast_days": days,
        "timezone": "auto"
    }
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
        print(f"Forecast fetch error: {e}")
        return None
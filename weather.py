import requests

from config_loader import config

LATITUDE = config["location"]["latitude"]
LONGITUDE = config["location"]["longitude"]

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_current_weather():
    """
    Returns a dict with cloud_cover (%), solar radiation values, and ambient
    temperature (C), or None on failure. Never raises - the main poll loop
    should keep running even if weather fails.
    """
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": "cloud_cover,shortwave_radiation,direct_radiation,diffuse_radiation,temperature_2m",
        "timezone": "auto"
    }
    try:
        response = requests.get(OPEN_METEO_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        current = data.get("current", {})
        return {
            "cloud_cover": current.get("cloud_cover"),
            "shortwave_radiation": current.get("shortwave_radiation"),
            "direct_radiation": current.get("direct_radiation"),
            "diffuse_radiation": current.get("diffuse_radiation"),
            "ambient_temp_c": current.get("temperature_2m"),
        }
    except Exception as e:
        print(f"Weather fetch error: {e}")
        return None
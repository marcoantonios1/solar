import pvlib
import pandas as pd
from config_loader import config

LATITUDE = config["location"]["latitude"]
LONGITUDE = config["location"]["longitude"]


def get_sun_position(timestamp=None):
    """
    Returns (elevation_deg, azimuth_deg) for the given timestamp (or now if None).
    Elevation: 0 = horizon, 90 = directly overhead.
    Azimuth: 0 = North, 90 = East, 180 = South, 270 = West.
    """
    if timestamp is None:
        timestamp = pd.Timestamp.now(tz="Asia/Beirut")

    solpos = pvlib.solarposition.get_solarposition(timestamp, LATITUDE, LONGITUDE)
    elevation = 90 - solpos["apparent_zenith"].iloc[0]
    azimuth = solpos["azimuth"].iloc[0]
    return elevation, azimuth


def get_sun_times(date=None):
    """Returns (sunrise, sunset) as timezone-aware timestamps for the given date (or today)."""
    if date is None:
        date = pd.Timestamp.now(tz="Asia/Beirut")
    sun_times = pvlib.solarposition.sun_rise_set_transit_spa(
        pd.DatetimeIndex([date]), LATITUDE, LONGITUDE
    )
    return sun_times["sunrise"].iloc[0], sun_times["sunset"].iloc[0]


if __name__ == "__main__":
    elevation, azimuth = get_sun_position()
    print(f"Current sun position: elevation={elevation:.2f} deg, azimuth={azimuth:.2f} deg")

    sunrise, sunset = get_sun_times()
    print(f"Today's sunrise: {sunrise}")
    print(f"Today's sunset: {sunset}")
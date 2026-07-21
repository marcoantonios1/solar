import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pvlib
import pandas as pd
from config_loader import config

LATITUDE = config["location"]["latitude"]
LONGITUDE = config["location"]["longitude"]
TILT = config["panels"]["tilt"]
AZIMUTH = config["panels"]["azimuth"]
ALTITUDE = 37  # site elevation in meters, from Open-Meteo response


def is_sun_obstructed(sun_elevation, sun_azimuth):
    """
    Checks whether the sun's current position falls behind a configured
    horizon obstruction (e.g. a neighboring building).
    Returns True if blocked, False otherwise.
    """
    obstructions = config.get("horizon_obstructions", [])
    for obstruction in obstructions:
        obs_azimuth = obstruction["azimuth"]
        obs_elevation = obstruction["elevation_angle_deg"]

        azimuth_diff = abs((sun_azimuth - obs_azimuth + 180) % 360 - 180)

        if azimuth_diff <= 15 and sun_elevation < obs_elevation:
            return True

    return False


def get_clearsky_poa_irradiance(timestamp=None):
    """
    Returns plane-of-array clear-sky irradiance (W/m^2) hitting the panels
    at the given timestamp, accounting for panel tilt, azimuth, and any
    configured horizon obstructions. This is the THEORETICAL maximum under
    clear sky - actual weather conditions will further reduce this.
    """
    if timestamp is None:
        timestamp = pd.Timestamp.now(tz="Asia/Beirut")

    ts_index = pd.DatetimeIndex([timestamp])

    solpos = pvlib.solarposition.get_solarposition(ts_index, LATITUDE, LONGITUDE, altitude=ALTITUDE)
    zenith = solpos["apparent_zenith"]
    sun_azimuth = solpos["azimuth"].iloc[0]
    sun_elevation = 90 - zenith.iloc[0]

    airmass_rel = pvlib.atmosphere.get_relative_airmass(zenith)
    pressure = pvlib.atmosphere.alt2pres(ALTITUDE)
    airmass_abs = pvlib.atmosphere.get_absolute_airmass(airmass_rel, pressure)
    turbidity = pvlib.clearsky.lookup_linke_turbidity(ts_index, LATITUDE, LONGITUDE)

    clearsky = pvlib.clearsky.ineichen(zenith, airmass_abs, turbidity, altitude=ALTITUDE)

    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=TILT,
        surface_azimuth=AZIMUTH,
        solar_zenith=zenith,
        solar_azimuth=solpos["azimuth"],
        dni=clearsky["dni"],
        ghi=clearsky["ghi"],
        dhi=clearsky["dhi"]
    )

    obstructed = is_sun_obstructed(sun_elevation, sun_azimuth)
    poa_global = 0.0 if obstructed else poa["poa_global"].iloc[0]

    return {
        "ghi": clearsky["ghi"].iloc[0],
        "dni": clearsky["dni"].iloc[0],
        "dhi": clearsky["dhi"].iloc[0],
        "poa_global": poa_global,
        "obstructed": obstructed,
        "sun_elevation": sun_elevation,
        "sun_azimuth": sun_azimuth,
    }


if __name__ == "__main__":
    result = get_clearsky_poa_irradiance()
    print(f"Panel tilt: {TILT} deg, azimuth: {AZIMUTH} deg")
    print(f"Sun elevation: {result['sun_elevation']:.1f} deg, azimuth: {result['sun_azimuth']:.1f} deg")
    print(f"Obstructed by building: {result['obstructed']}")
    print(f"Clear-sky GHI (horizontal): {result['ghi']:.1f} W/m^2")
    print(f"Plane-of-array irradiance (on panels): {result['poa_global']:.1f} W/m^2")
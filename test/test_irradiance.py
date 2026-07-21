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


def get_clearsky_poa_irradiance(timestamp=None):
    """
    Returns plane-of-array clear-sky irradiance (W/m^2) hitting the panels
    at the given timestamp, accounting for panel tilt and azimuth.
    This is the THEORETICAL maximum under clear sky - actual conditions
    (clouds, obstructions) will reduce this.
    """
    if timestamp is None:
        timestamp = pd.Timestamp.now(tz="Asia/Beirut")

    ts_index = pd.DatetimeIndex([timestamp])

    solpos = pvlib.solarposition.get_solarposition(ts_index, LATITUDE, LONGITUDE, altitude=ALTITUDE)
    zenith = solpos["apparent_zenith"]
    sun_azimuth = solpos["azimuth"]

    airmass_rel = pvlib.atmosphere.get_relative_airmass(zenith)
    pressure = pvlib.atmosphere.alt2pres(ALTITUDE)
    airmass_abs = pvlib.atmosphere.get_absolute_airmass(airmass_rel, pressure)
    turbidity = pvlib.clearsky.lookup_linke_turbidity(ts_index, LATITUDE, LONGITUDE)

    clearsky = pvlib.clearsky.ineichen(zenith, airmass_abs, turbidity, altitude=ALTITUDE)

    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=TILT,
        surface_azimuth=AZIMUTH,
        solar_zenith=zenith,
        solar_azimuth=sun_azimuth,
        dni=clearsky["dni"],
        ghi=clearsky["ghi"],
        dhi=clearsky["dhi"]
    )

    return {
        "ghi": clearsky["ghi"].iloc[0],
        "dni": clearsky["dni"].iloc[0],
        "dhi": clearsky["dhi"].iloc[0],
        "poa_global": poa["poa_global"].iloc[0],
    }


if __name__ == "__main__":
    result = get_clearsky_poa_irradiance()
    print(f"Panel tilt: {TILT} deg, azimuth: {AZIMUTH} deg")
    print(f"Clear-sky GHI (horizontal): {result['ghi']:.1f} W/m^2")
    print(f"Clear-sky DNI: {result['dni']:.1f} W/m^2")
    print(f"Clear-sky DHI: {result['dhi']:.1f} W/m^2")
    print(f"Plane-of-array irradiance (on panels): {result['poa_global']:.1f} W/m^2")
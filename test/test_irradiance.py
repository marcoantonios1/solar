import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pvlib
import pandas as pd
from config_loader import config
from datetime import datetime

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


def estimate_panel_temp(ambient_temp_c, poa_irradiance, noct=45):
    """
    Estimates panel temperature using the standard NOCT approximation.
    NOCT (Nominal Operating Cell Temp) default of 45C is typical for most panels.
    """
    return ambient_temp_c + ((noct - 20) / 800) * poa_irradiance


def get_expected_power(ambient_temp_c, timestamp=None):
    """
    Returns expected power output (W) for the full array, accounting for:
    - Plane-of-array irradiance (tilt/azimuth)
    - Horizon obstruction shading
    - Panel temperature derating
    - Age-based degradation since installation
    """
    irradiance = get_clearsky_poa_irradiance(timestamp)
    poa = irradiance["poa_global"]

    rated_watts_per_panel = config["panels"]["rated_watts"]
    panel_count = config["panels"]["count"]
    temp_coefficient = config["panels"]["temp_coefficient"]
    degradation_rate = config["panels"]["annual_degradation_rate"]
    install_date = datetime.strptime(config["panels"]["installation_date"], "%Y-%m-%d")

    total_rated_watts = rated_watts_per_panel * panel_count

    panel_temp = estimate_panel_temp(ambient_temp_c, poa)
    temp_factor = 1 + temp_coefficient * (panel_temp - 25)

    years_since_install = (datetime.now() - install_date).days / 365.25
    degradation_factor = 1 - (degradation_rate * years_since_install)

    irradiance_ratio = poa / 1000  # STC reference is 1000 W/m^2

    expected_power = total_rated_watts * irradiance_ratio * temp_factor * degradation_factor

    return {
        "poa_irradiance": poa,
        "obstructed": irradiance["obstructed"],
        "panel_temp_c": panel_temp,
        "temp_factor": temp_factor,
        "degradation_factor": degradation_factor,
        "years_since_install": years_since_install,
        "expected_power_w": max(expected_power, 0),
    }


if __name__ == "__main__":
    result = get_clearsky_poa_irradiance()
    print(f"Panel tilt: {TILT} deg, azimuth: {AZIMUTH} deg")
    print(f"Sun elevation: {result['sun_elevation']:.1f} deg, azimuth: {result['sun_azimuth']:.1f} deg")
    print(f"Obstructed by building: {result['obstructed']}")
    print(f"Clear-sky GHI (horizontal): {result['ghi']:.1f} W/m^2")
    print(f"Plane-of-array irradiance (on panels): {result['poa_global']:.1f} W/m^2")

    full_result = get_expected_power(ambient_temp_c=28)
    print()
    print(f"Panel temp estimate: {full_result['panel_temp_c']:.1f} C")
    print(f"Temp derating factor: {full_result['temp_factor']:.4f}")
    print(f"Degradation factor ({full_result['years_since_install']:.1f} years old): {full_result['degradation_factor']:.4f}")
    print(f"Full expected power: {full_result['expected_power_w']:.1f} W")
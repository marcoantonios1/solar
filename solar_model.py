import pvlib
import pandas as pd
from datetime import datetime

from config_loader import config

LATITUDE = config["location"]["latitude"]
LONGITUDE = config["location"]["longitude"]
TILT = config["panels"]["tilt"]
AZIMUTH = config["panels"]["azimuth"]
ALTITUDE = 37  # site elevation in meters, from Open-Meteo response


def is_sun_obstructed(sun_elevation, sun_azimuth):
    obstructions = config.get("horizon_obstructions", [])
    for obstruction in obstructions:
        obs_azimuth = obstruction["azimuth"]
        obs_elevation = obstruction["elevation_angle_deg"]
        azimuth_diff = abs((sun_azimuth - obs_azimuth + 180) % 360 - 180)
        if azimuth_diff <= 15 and sun_elevation < obs_elevation:
            return True
    return False


def get_clearsky_poa_irradiance(timestamp=None):
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
    return ambient_temp_c + ((noct - 20) / 800) * poa_irradiance


def get_expected_power(ambient_temp_c, timestamp=None):
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

    irradiance_ratio = poa / 1000

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
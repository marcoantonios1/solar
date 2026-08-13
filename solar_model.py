import pvlib
import pandas as pd
from datetime import datetime

from config_loader import config

LATITUDE = config["location"]["latitude"]
LONGITUDE = config["location"]["longitude"]
TILT = config["panels"]["tilt"]
AZIMUTH = config["panels"]["azimuth"]
ALTITUDE = config["location"].get("altitude_m", 0)  # Default to 0 if not specified
TEMPORARY_PERFORMANCE_DERATE = config["panels"].get("temporary_performance_derate", 1.0)


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

    raw_expected_power = total_rated_watts * irradiance_ratio * temp_factor * degradation_factor
    expected_power = raw_expected_power * TEMPORARY_PERFORMANCE_DERATE

    return {
        "poa_irradiance": poa,
        "obstructed": irradiance["obstructed"],
        "panel_temp_c": panel_temp,
        "temp_factor": temp_factor,
        "raw_expected_power_w": max(raw_expected_power, 0),
        "degradation_factor": degradation_factor,
        "years_since_install": years_since_install,
        "expected_power_w": max(expected_power, 0),
    }

def get_weather_adjusted_poa_irradiance(ghi, dni, dhi, timestamp=None):
    """
    Projects REAL, already cloud-adjusted GHI/DNI/DHI (from Open-Meteo) onto
    the panel plane. Unlike get_clearsky_poa_irradiance, this reflects actual
    or forecast sky conditions, not a hypothetical clear sky - this is what
    Phase 4's prediction layers should use.
    """
    if timestamp is None:
        timestamp = pd.Timestamp.now(tz="Asia/Beirut")

    ts_index = pd.DatetimeIndex([timestamp])
    solpos = pvlib.solarposition.get_solarposition(ts_index, LATITUDE, LONGITUDE, altitude=ALTITUDE)
    zenith = solpos["apparent_zenith"]
    sun_azimuth = solpos["azimuth"].iloc[0]
    sun_elevation = 90 - zenith.iloc[0]

    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=TILT,
        surface_azimuth=AZIMUTH,
        solar_zenith=zenith,
        solar_azimuth=solpos["azimuth"],
        dni=dni if dni is not None else 0,
        ghi=ghi if ghi is not None else 0,
        dhi=dhi if dhi is not None else 0
    )

    obstructed = is_sun_obstructed(sun_elevation, sun_azimuth)
    poa_global = 0.0 if obstructed else poa["poa_global"].iloc[0]

    return {
        "poa_global": poa_global,
        "obstructed": obstructed,
        "sun_elevation": sun_elevation,
        "sun_azimuth": sun_azimuth,
    }


def get_weather_adjusted_expected_power(ghi, dni, dhi, ambient_temp_c, timestamp=None):
    """
    Same as get_expected_power, but using real weather-service irradiance
    (already cloud-adjusted) instead of the clear-sky model.
    """
    irradiance = get_weather_adjusted_poa_irradiance(ghi, dni, dhi, timestamp)
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

    expected_power = total_rated_watts * irradiance_ratio * temp_factor * degradation_factor * TEMPORARY_PERFORMANCE_DERATE

    return {
        "poa_irradiance": poa,
        "obstructed": irradiance["obstructed"],
        "panel_temp_c": panel_temp,
        "temp_factor": temp_factor,
        "degradation_factor": degradation_factor,
        "expected_power_w": max(expected_power, 0),
    }

def get_sun_times_for_date(date_str):
    """
    Returns (sunrise, sunset) as timezone-aware pandas Timestamps for the
    given date string (YYYY-MM-DD).
    """
    ts = pd.Timestamp(date_str, tz="Asia/Beirut").replace(hour=12)
    sun_times = pvlib.solarposition.sun_rise_set_transit_spa(
        pd.DatetimeIndex([ts]), LATITUDE, LONGITUDE
    )
    return sun_times["sunrise"].iloc[0], sun_times["sunset"].iloc[0]
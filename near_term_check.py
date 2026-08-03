import pandas as pd

from config_loader import config
from weather import fetch_forecast_weather
from solar_model import get_weather_adjusted_expected_power, get_sun_times_for_date
from load_model import get_expected_load
from output_mode_manager import classify_energy_balance

CAPACITY_KWH_USABLE = config["battery"]["capacity_kwh_usable"]


def get_current_soc(conn):
    row = conn.execute(
        "SELECT battery_soc FROM readings WHERE battery_soc IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def get_remaining_solar_kwh(now, sunset):
    forecast = fetch_forecast_weather(days=1)
    if forecast is None:
        return None

    total_kwh = 0.0
    for i, time_str in enumerate(forecast["time"]):
        timestamp = pd.Timestamp(time_str, tz="Asia/Beirut")
        if now <= timestamp <= sunset:
            ghi, dni, dhi, temp = forecast["ghi"][i], forecast["dni"][i], forecast["dhi"][i], forecast["temp"][i]
            if any(v is None for v in [ghi, dni, dhi, temp]):
                continue
            result = get_weather_adjusted_expected_power(
                ghi=ghi, dni=dni, dhi=dhi, ambient_temp_c=temp, timestamp=timestamp
            )
            total_kwh += result["expected_power_w"] / 1000

    return round(total_kwh, 2)


def get_battery_projection(conn):
    """
    Daytime-only check: recomputes today's energy balance using LIVE current
    SOC and a short-range forecast, feeding it through the SAME
    classify_energy_balance() tier logic Layer 1 uses - not a separate
    calculation. Returns None if outside daylight hours or no SOC data.
    """
    now = pd.Timestamp.now(tz="Asia/Beirut")
    today_str = now.strftime("%Y-%m-%d")
    sunrise, sunset = get_sun_times_for_date(today_str)

    if not (sunrise <= now <= sunset):
        return None  # outside daylight hours - Layer 2 does nothing

    current_soc = get_current_soc(conn)
    if current_soc is None:
        return None

    current_kwh = (current_soc / 100) * CAPACITY_KWH_USABLE
    solar_result = get_remaining_solar_kwh(now, sunset)
    fetch_failed = solar_result is None
    remaining_solar_kwh = solar_result if solar_result is not None else 0
    remaining_hours = (sunset - now).total_seconds() / 3600

    load_estimate = get_expected_load(conn)
    remaining_house_kwh = (load_estimate["day_load_w"] * remaining_hours) / 1000

    charger_mode, output_priority, label, balance_kwh = classify_energy_balance(
        solar_expected_kwh=remaining_solar_kwh,
        battery_available_kwh=current_kwh,
        house_expected_kwh=remaining_house_kwh,
    )

    return {
        "current_soc_pct": current_soc,
        "current_kwh": round(current_kwh, 2),
        "remaining_solar_kwh": remaining_solar_kwh,
        "remaining_hours": round(remaining_hours, 2),
        "remaining_house_kwh": round(remaining_house_kwh, 2),
        "balance_kwh": round(balance_kwh, 2),
        "fetch_failed": fetch_failed,
        "charger_mode": charger_mode,
        "output_priority": output_priority,
        "label": label,
    }
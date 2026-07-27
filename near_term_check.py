import pandas as pd

from config_loader import config
from weather import fetch_forecast_weather
from solar_model import get_weather_adjusted_expected_power, get_sun_times_for_date
from load_model import get_expected_load

CAPACITY_KWH_USABLE = config["battery"]["capacity_kwh_usable"]
CRITICAL_SOC_FLOOR = config["thresholds"]["low_soc_threshold"]
MIN_MEANINGFUL_HOURS_GAINED = 1.0


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
    Daytime-only check: projects whether the battery will reach full charge
    by tonight's sunset, using LIVE current SOC. Returns None if it's
    currently outside daylight hours - Layer 2 has no role at night.
    Whatever mode is active at sunset (Layer 1's morning decision, or a
    daytime escalation from this check) carries through the whole night
    unchanged, since only Layer 1's next daily run should ever relax it
    back down.
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
    remaining_solar_kwh = get_remaining_solar_kwh(now, sunset) or 0
    remaining_hours = (sunset - now).total_seconds() / 3600

    load_estimate = get_expected_load(conn)
    remaining_house_kwh = (load_estimate["day_load_w"] * remaining_hours) / 1000

    projected_kwh = current_kwh + remaining_solar_kwh - remaining_house_kwh

    critical_floor_kwh = (CRITICAL_SOC_FLOOR / 100) * CAPACITY_KWH_USABLE
    night_load_kwh_per_hour = load_estimate["night_load_w"] / 1000

    # Hours the battery would last from SUNSET (using projected_kwh - the
    # estimated state AT sunset, not right now) before hitting the critical
    # floor - both without any more charging (current trajectory) and if
    # topped up to near-full by escalating. This is the real question (does
    # escalating meaningfully delay/avoid Rule 1 firing tonight), not
    # "did we reach 100%".
    hours_until_critical_without_escalating = (projected_kwh - critical_floor_kwh) / night_load_kwh_per_hour
    hours_until_critical_if_escalated = (CAPACITY_KWH_USABLE - critical_floor_kwh) / night_load_kwh_per_hour

    next_sunrise, _ = get_sun_times_for_date((now + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    hours_until_sunrise = (next_sunrise - sunset).total_seconds() / 3600

    survives_night_without_charging = hours_until_critical_without_escalating >= hours_until_sunrise
    hours_gained_if_escalated = (
        min(hours_until_critical_if_escalated, hours_until_sunrise) -
        min(hours_until_critical_without_escalating, hours_until_sunrise)
    )
    closes_gap_entirely = hours_until_critical_if_escalated >= hours_until_sunrise and not survives_night_without_charging

    worth_escalating = (not survives_night_without_charging) and (closes_gap_entirely or hours_gained_if_escalated >= MIN_MEANINGFUL_HOURS_GAINED)

    return {
        "current_soc_pct": current_soc,
        "current_kwh": round(current_kwh, 2),
        "remaining_solar_kwh": remaining_solar_kwh,
        "remaining_hours": round(remaining_hours, 2),
        "remaining_house_kwh": round(remaining_house_kwh, 2),
        "projected_kwh": round(projected_kwh, 2),
        "hours_until_critical_without_escalating": round(hours_until_critical_without_escalating, 2),
        "hours_gained_if_escalated": round(hours_gained_if_escalated, 2),
        "survives_night_without_charging": survives_night_without_charging,
        "worth_escalating": worth_escalating,
    }
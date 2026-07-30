import pandas as pd

from config_loader import config
from daily_forecast import get_7day_solar_forecast
from load_model import get_expected_load
from battery_model import get_battery_available_kwh
from energy_balance import calculate_energy_balance
from solar_model import get_sun_times_for_date

CAPACITY_KWH_USABLE = config["battery"]["capacity_kwh_usable"]


def get_current_soc(conn):
    """Returns the most recent logged battery_soc, or None if no readings exist."""
    row = conn.execute(
        "SELECT battery_soc FROM readings WHERE battery_soc IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def get_daily_predictions(conn):
    forecast = get_7day_solar_forecast()
    if forecast is None:
        return None

    today_str = pd.Timestamp.now(tz="Asia/Beirut").strftime("%Y-%m-%d")
    load_estimate = get_expected_load(conn)
    current_soc = get_current_soc(conn)

    predictions = []
    chained_battery_kwh = None  # carries forward from each day's ending state

    for date_str in sorted(forecast.keys()):
        solar_expected_kwh = forecast[date_str]["expected_kwh"]

        sunrise, sunset = get_sun_times_for_date(date_str)
        next_day = (pd.Timestamp(date_str) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        next_sunrise, _ = get_sun_times_for_date(next_day)

        day_hours = (sunset - sunrise).total_seconds() / 3600
        night_hours = (next_sunrise - sunset).total_seconds() / 3600

        house_expected_kwh = (
            (load_estimate["day_load_w"] * day_hours) +
            (load_estimate["night_load_w"] * night_hours)
        ) / 1000

        if date_str == today_str:
            battery_info = get_battery_available_kwh(conn, date_str=today_str)
            battery_available_kwh = battery_info["battery_available_kwh"] or 0
            battery_source = battery_info["source"]
        elif chained_battery_kwh is not None:
            battery_available_kwh = chained_battery_kwh
            battery_source = "chained_from_previous_day"
        else:
            # Fallback: today's real reading was unavailable, so nothing to chain from
            battery_available_kwh = 0
            battery_source = "conservative_zero_assumption"

        balance = calculate_energy_balance(
            solar_expected_kwh=solar_expected_kwh,
            battery_available_kwh=battery_available_kwh,
            house_expected_kwh=round(house_expected_kwh, 2)
        )

        # This day's predicted ending battery state feeds tomorrow's starting point -
        # clamped to what's physically possible (can't go negative, can't exceed capacity)
        chained_battery_kwh = max(0, min(balance["balance_kwh"], CAPACITY_KWH_USABLE))

        battery_recharge_status = None
        if date_str == today_str and current_soc is not None and balance["classification"] == "surplus":
            kwh_needed_to_full = (1 - current_soc / 100) * CAPACITY_KWH_USABLE
            net_after_recharge = balance["balance_kwh"] - kwh_needed_to_full
            battery_recharge_status = {
                "current_soc_pct": current_soc,
                "kwh_needed_to_full": round(kwh_needed_to_full, 2),
                "net_after_recharge_kwh": round(net_after_recharge, 2),
                "will_reach_full": net_after_recharge >= 0,
            }

        predictions.append({
            "date": date_str,
            "solar_expected_kwh": solar_expected_kwh,
            "house_expected_kwh": round(house_expected_kwh, 2),
            "battery_available_kwh": round(battery_available_kwh, 2),
            "battery_source": battery_source,
            "balance_kwh": balance["balance_kwh"],
            "classification": balance["classification"],
            "shortfall_kwh": balance["shortfall_kwh"],
            "battery_recharge_status": battery_recharge_status,
        })

    return predictions
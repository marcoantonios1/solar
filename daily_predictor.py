import pandas as pd

from config_loader import config
from daily_forecast import get_7day_solar_forecast
from load_model import get_expected_load
from battery_model import get_battery_available_kwh
from energy_balance import calculate_energy_balance
from solar_model import get_sun_times_for_date

CAPACITY_KWH_USABLE = config["battery"]["capacity_kwh_usable"]


def get_daily_predictions(conn):
    """
    Returns a list of predictions for each of the next 7 forecast days:
    date, solar_expected_kwh, house_expected_kwh, battery_available_kwh,
    balance_kwh, classification, shortfall_kwh.

    Today uses the real pre-sunrise battery reading. Future days use a
    conservative battery_available_kwh=0 assumption, since we can't know
    their actual starting SOC in advance - a shortfall flagged under that
    assumption is a real risk signal, not a guaranteed outcome.
    """
    forecast = get_7day_solar_forecast()
    if forecast is None:
        return None

    today_str = pd.Timestamp.now(tz="Asia/Beirut").strftime("%Y-%m-%d")
    load_estimate = get_expected_load(conn)

    predictions = []

    for i, date_str in enumerate(sorted(forecast.keys())):
        solar_expected_kwh = forecast[date_str]["expected_kwh"]

        sunrise, sunset = get_sun_times_for_date(date_str)
        day_hours = (sunset - sunrise).total_seconds() / 3600
        night_hours = 24 - day_hours

        house_expected_kwh = (
            (load_estimate["day_load_w"] * day_hours) +
            (load_estimate["night_load_w"] * night_hours)
        ) / 1000

        if date_str == today_str:
            battery_info = get_battery_available_kwh(conn, date_str=today_str)
            battery_available_kwh = battery_info["battery_available_kwh"] or 0
            battery_source = battery_info["source"]
        else:
            battery_available_kwh = 0
            battery_source = "conservative_zero_assumption"

        balance = calculate_energy_balance(
            solar_expected_kwh=solar_expected_kwh,
            battery_available_kwh=battery_available_kwh,
            house_expected_kwh=round(house_expected_kwh, 2)
        )

        predictions.append({
            "date": date_str,
            "solar_expected_kwh": solar_expected_kwh,
            "house_expected_kwh": round(house_expected_kwh, 2),
            "battery_available_kwh": battery_available_kwh,
            "battery_source": battery_source,
            "balance_kwh": balance["balance_kwh"],
            "classification": balance["classification"],
            "shortfall_kwh": balance["shortfall_kwh"],
        })

    return predictions
from collections import defaultdict
import pandas as pd

from weather import fetch_forecast_weather
from solar_model import get_weather_adjusted_expected_power


def get_7day_solar_forecast(days=7):
    """
    Returns a dict mapping each of the next `days` dates (YYYY-MM-DD) to
    predicted solar energy for that day, in kWh. Runs the weather-adjusted
    pvlib model against each hour of the forecast, then sums each day's
    hourly expected-power values (each ~1hr bucket) into a daily kWh total.
    """
    forecast = fetch_forecast_weather(days=days)
    if forecast is None:
        return None

    hourly_kwh_by_date = defaultdict(float)
    hours_counted_by_date = defaultdict(int)

    for i, time_str in enumerate(forecast["time"]):
        ghi = forecast["ghi"][i]
        dni = forecast["dni"][i]
        dhi = forecast["dhi"][i]
        temp = forecast["temp"][i]

        if any(v is None for v in [ghi, dni, dhi, temp]):
            continue

        timestamp = pd.Timestamp(time_str, tz="Asia/Beirut")

        result = get_weather_adjusted_expected_power(
            ghi=ghi, dni=dni, dhi=dhi, ambient_temp_c=temp, timestamp=timestamp
        )

        date_str = timestamp.strftime("%Y-%m-%d")
        # Each hourly forecast point represents ~1 hour of energy at that power level
        hourly_kwh_by_date[date_str] += result["expected_power_w"] / 1000
        hours_counted_by_date[date_str] += 1

    return {
        date: {
            "expected_kwh": round(kwh, 2),
            "hours_counted": hours_counted_by_date[date],
        }
        for date, kwh in sorted(hourly_kwh_by_date.items())
    }
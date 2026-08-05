from collections import defaultdict
import pandas as pd

from weather import fetch_forecast_weather, parse_forecast_timestamp
from solar_model import get_weather_adjusted_expected_power, get_sun_times_for_date


def get_7day_solar_forecast(days=8):
    """
    Returns a dict mapping each of the next 7 sunrise-to-next-sunrise CYCLES
    (keyed by the cycle's starting date) to predicted solar energy, in kWh.
    A "cycle" runs from one day's sunrise to the next day's sunrise - this
    matches how battery_available is measured (at sunrise) and represents
    one full day+night period, rather than a calendar midnight-to-midnight
    day which would mismatch the battery measurement point.

    Fetches days+1 days of forecast so the last cycle has a full night to draw from.
    """
    forecast = fetch_forecast_weather(days=days)
    if forecast is None:
        return None

    # Pre-compute sunrise times for each calendar date covered, so we can
    # bucket hourly forecast points into the correct sunrise-to-sunrise cycle
    unique_dates = sorted(set(t[:10] for t in forecast["time"]))
    sunrise_by_date = {d: get_sun_times_for_date(d)[0] for d in unique_dates}
    sorted_sunrises = sorted(sunrise_by_date.items(), key=lambda x: x[1])

    cycle_kwh = defaultdict(float)
    cycle_hours = defaultdict(int)

    for i, time_str in enumerate(forecast["time"]):
        ghi = forecast["ghi"][i]
        dni = forecast["dni"][i]
        dhi = forecast["dhi"][i]
        temp = forecast["temp"][i]

        if any(v is None for v in [ghi, dni, dhi, temp]):
            continue

        timestamp = parse_forecast_timestamp(time_str)
        if timestamp is None:
            continue  # DST edge case or unparseable - skip this hour, don't crash the run

        # Find which sunrise-to-sunrise cycle this hour belongs to:
        # the most recent sunrise at or before this timestamp starts its cycle.
        cycle_start_date = None
        for date_str, sunrise in sorted_sunrises:
            if sunrise <= timestamp:
                cycle_start_date = date_str
            else:
                break

        if cycle_start_date is None:
            continue  # before the first known sunrise in our data, skip

        result = get_weather_adjusted_expected_power(
            ghi=ghi, dni=dni, dhi=dhi, ambient_temp_c=temp, timestamp=timestamp
        )

        cycle_kwh[cycle_start_date] += result["expected_power_w"] / 1000
        cycle_hours[cycle_start_date] += 1

    return {
        date: {
            "expected_kwh": round(kwh, 2),
            "hours_counted": cycle_hours[date],
        }
        for date, kwh in sorted(cycle_kwh.items())
    }
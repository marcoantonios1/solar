from datetime import datetime
import pandas as pd

from config_loader import config
from solar_model import get_sun_times_for_date

SEASONAL_ESTIMATE = config["seasonal_load_estimate"]
SEASON_MONTHS = SEASONAL_ESTIMATE["months"]

MIN_DAYS_FOR_HISTORICAL_TRUST = 7


def get_season(month=None):
    if month is None:
        month = datetime.now().month

    for season, months in SEASON_MONTHS.items():
        if month in months:
            return season

    return "summer"


def get_seasonal_fallback_night_load(season):
    return SEASONAL_ESTIMATE[f"{season}_night_avg_load_w"]


def get_historical_load(conn, season):
    """
    Returns day/night/all-day average load (W) for the given season, using
    REAL sunrise/sunset per date (via pvlib) to classify each reading as
    day or night, rather than a fixed hour boundary.
    """
    months = SEASON_MONTHS[season]
    month_conditions = " OR ".join([f"CAST(strftime('%m', timestamp) AS INTEGER) = {m}" for m in months])

    rows = conn.execute(
        f"""SELECT load_power, timestamp FROM readings
            WHERE ({month_conditions}) AND load_power IS NOT NULL"""
    ).fetchall()

    if not rows:
        return {
            "day_avg_w": None, "night_avg_w": None, "all_avg_w": None,
            "day_days": 0, "night_days": 0, "all_days": 0,
        }

    sun_times_cache = {}
    day_rows = []
    night_rows = []

    for load_power, timestamp in rows:
        date_str = timestamp[:10]

        if date_str not in sun_times_cache:
            sun_times_cache[date_str] = get_sun_times_for_date(date_str)
        sunrise, sunset = sun_times_cache[date_str]

        reading_ts = pd.Timestamp(timestamp, tz="Asia/Beirut")

        if sunrise <= reading_ts <= sunset:
            day_rows.append((load_power, date_str))
        else:
            night_rows.append((load_power, date_str))

    def avg_and_days(subset):
        if not subset:
            return None, 0
        avg = sum(r[0] for r in subset) / len(subset)
        days = len(set(r[1] for r in subset))
        return avg, days

    day_avg, day_days = avg_and_days(day_rows)
    night_avg, night_days = avg_and_days(night_rows)
    all_avg, all_days = avg_and_days([(r[0], r[1]) for r in rows])

    return {
        "day_avg_w": day_avg, "night_avg_w": night_avg, "all_avg_w": all_avg,
        "day_days": day_days, "night_days": night_days, "all_days": all_days,
    }


def get_expected_load(conn, month=None):
    season = get_season(month)
    hist = get_historical_load(conn, season)

    result = {"season": season}

    if hist["night_avg_w"] is not None and hist["night_days"] >= MIN_DAYS_FOR_HISTORICAL_TRUST:
        result["night_load_w"] = hist["night_avg_w"]
        result["night_source"] = "historical"
        result["night_days"] = hist["night_days"]
    else:
        result["night_load_w"] = get_seasonal_fallback_night_load(season)
        result["night_source"] = "config_fallback"
        result["night_days"] = hist["night_days"]

    if hist["day_avg_w"] is not None and hist["day_days"] >= MIN_DAYS_FOR_HISTORICAL_TRUST:
        result["day_load_w"] = hist["day_avg_w"]
        result["day_source"] = "historical"
        result["day_days"] = hist["day_days"]
    else:
        result["day_load_w"] = get_seasonal_fallback_night_load(season) * 0.4
        result["day_source"] = "rough_placeholder"
        result["day_days"] = hist["day_days"]

    return result
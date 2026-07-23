from datetime import datetime

from config_loader import config

SEASONAL_ESTIMATE = config["seasonal_load_estimate"]
SEASON_MONTHS = SEASONAL_ESTIMATE["months"]

MIN_DAYS_FOR_HISTORICAL_TRUST = 7

NIGHT_START_HOUR = 20  # 20:00
NIGHT_END_HOUR = 7     # 07:00 - readings with hour >= NIGHT_START_HOUR or < NIGHT_END_HOUR count as "night"


def get_season(month=None):
    if month is None:
        month = datetime.now().month

    for season, months in SEASON_MONTHS.items():
        if month in months:
            return season

    return "summer"


def get_seasonal_fallback_night_load(season):
    """Returns the config-defined rough average NIGHT load (W) for a season."""
    return SEASONAL_ESTIMATE[f"{season}_night_avg_load_w"]


def get_historical_load(conn, season):
    """
    Returns day/night/all-day average load (W) for the given season, using
    all readings logged so far across any year. Returns None values if no
    data exists yet. Also returns distinct_days for each period, so callers
    can decide whether there's enough data to trust it.
    """
    months = SEASON_MONTHS[season]
    month_conditions = " OR ".join([f"CAST(strftime('%m', timestamp) AS INTEGER) = {m}" for m in months])

    rows = conn.execute(
        f"""SELECT load_power, timestamp, CAST(strftime('%H', timestamp) AS INTEGER) as hour
            FROM readings
            WHERE ({month_conditions}) AND load_power IS NOT NULL"""
    ).fetchall()

    if not rows:
        return {
            "day_avg_w": None, "night_avg_w": None, "all_avg_w": None,
            "day_days": 0, "night_days": 0, "all_days": 0,
        }

    day_rows = [r for r in rows if NIGHT_END_HOUR <= r[2] < NIGHT_START_HOUR]
    night_rows = [r for r in rows if r[2] >= NIGHT_START_HOUR or r[2] < NIGHT_END_HOUR]

    def avg_and_days(subset):
        if not subset:
            return None, 0
        avg = sum(r[0] for r in subset) / len(subset)
        days = len(set(r[1][:10] for r in subset))
        return avg, days

    day_avg, day_days = avg_and_days(day_rows)
    night_avg, night_days = avg_and_days(night_rows)
    all_avg, all_days = avg_and_days(rows)

    return {
        "day_avg_w": day_avg, "night_avg_w": night_avg, "all_avg_w": all_avg,
        "day_days": day_days, "night_days": night_days, "all_days": all_days,
    }


def get_expected_load(conn, month=None):
    """
    Returns the best available estimate of typical house load (W) for the
    given month's season, split into day/night/all-day. Prefers real
    historical data per-period once enough exists (MIN_DAYS_FOR_HISTORICAL_TRUST
    days), otherwise falls back to config for night, and a rough placeholder
    for day (daytime load matters less since solar is directly available then).
    """
    season = get_season(month)
    hist = get_historical_load(conn, season)

    result = {"season": season}

    # Night: grounded config fallback available
    if hist["night_avg_w"] is not None and hist["night_days"] >= MIN_DAYS_FOR_HISTORICAL_TRUST:
        result["night_load_w"] = hist["night_avg_w"]
        result["night_source"] = "historical"
        result["night_days"] = hist["night_days"]
    else:
        result["night_load_w"] = get_seasonal_fallback_night_load(season)
        result["night_source"] = "config_fallback"
        result["night_days"] = hist["night_days"]

    # Day: no grounded fallback yet - use a rough placeholder (60% of night estimate,
    # a loose guess) until real data accumulates. Flagged clearly via source field.
    if hist["day_avg_w"] is not None and hist["day_days"] >= MIN_DAYS_FOR_HISTORICAL_TRUST:
        result["day_load_w"] = hist["day_avg_w"]
        result["day_source"] = "historical"
        result["day_days"] = hist["day_days"]
    else:
        result["day_load_w"] = get_seasonal_fallback_night_load(season) * 0.40
        result["day_source"] = "rough_placeholder"
        result["day_days"] = hist["day_days"]

    return result
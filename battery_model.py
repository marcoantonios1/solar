import pandas as pd

from config_loader import config
from solar_model import get_sun_times_for_date

CAPACITY_KWH_USABLE = config["battery"]["capacity_kwh_usable"]


def get_pre_sunrise_soc(conn, date_str=None):
    """
    Returns (soc_pct, timestamp) for the lowest battery_soc reading in the
    hour before sunrise on the given date (or today if not specified).
    This represents the true overnight low - the real starting buffer for
    the day, before solar starts recovering it. Returns (None, None) if no
    readings exist in that window.
    """
    if date_str is None:
        date_str = pd.Timestamp.now(tz="Asia/Beirut").strftime("%Y-%m-%d")

    sunrise, _ = get_sun_times_for_date(date_str)
    window_start = sunrise - pd.Timedelta(hours=1)

    rows = conn.execute(
        """SELECT battery_soc, timestamp FROM readings
           WHERE timestamp >= ? AND timestamp <= ?
           AND battery_soc IS NOT NULL
           ORDER BY battery_soc ASC LIMIT 1""",
        (window_start.strftime("%Y-%m-%dT%H:%M:%S"), sunrise.strftime("%Y-%m-%dT%H:%M:%S"))
    ).fetchone()

    if rows is None:
        return None, None

    return rows[0], rows[1]


def get_battery_available_kwh(conn, date_str=None):
    """
    Returns the true available battery buffer (kWh) for the given date,
    based on the pre-sunrise SOC low. Returns None if no data exists for
    that window.
    """
    soc_pct, timestamp = get_pre_sunrise_soc(conn, date_str)

    if soc_pct is None:
        return {
            "battery_available_kwh": None,
            "soc_pct": None,
            "timestamp": None,
            "source": "no_data",
        }

    available_kwh = (soc_pct / 100) * CAPACITY_KWH_USABLE

    return {
        "battery_available_kwh": round(available_kwh, 2),
        "soc_pct": soc_pct,
        "timestamp": timestamp,
        "source": "pre_sunrise_reading",
    }
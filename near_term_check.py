import pandas as pd

from solar_model import get_sun_times_for_date
from pipeline import run_pipeline


def get_battery_projection(conn):
    """
    Daytime-only Layer 2 check, built on the shared pipeline (Issue #150).
    A failed provider inside the pipeline already returns None - there's
    no separate "fetch_failed" check needed here anymore, since it's
    structurally impossible to get a bad proposal out, not just checked for.
    """
    now = pd.Timestamp.now(tz="Asia/Beirut")
    today_str = now.strftime("%Y-%m-%d")
    sunrise, sunset = get_sun_times_for_date(today_str)

    if not (sunrise <= now <= sunset):
        return None  # outside daylight hours - Layer 2 does nothing

    return run_pipeline(conn, now, sunset, source="layer2")
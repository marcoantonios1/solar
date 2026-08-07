from dataclasses import dataclass
from datetime import datetime

from config_loader import config
from load_model import get_expected_load

CAPACITY_KWH_USABLE = config["battery"]["capacity_kwh_usable"]


@dataclass(frozen=True)
class ProviderResult:
    """
    A single data point from a provider, with provenance - so downstream
    pipeline stages can check freshness/reliability, not just trust a bare
    number. `failed=True` means the underlying data source didn't respond;
    value_kwh will be 0 in that case, but callers must check `failed`
    rather than assuming 0 means "genuinely zero, nothing available".
    """
    value_kwh: float
    source: str
    fetched_at: datetime
    failed: bool = False


def battery_state(conn):
    """
    Provider: current battery energy (kWh), from the most recent live
    reading. The simplest provider - single data source, single failure
    mode (no recent reading exists).
    """
    row = conn.execute(
        "SELECT battery_soc, timestamp FROM readings WHERE battery_soc IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()

    if row is None:
        return ProviderResult(value_kwh=0, source="no_reading_available", fetched_at=datetime.now(), failed=True)

    soc_pct, timestamp_str = row
    kwh = (soc_pct / 100) * CAPACITY_KWH_USABLE

    return ProviderResult(value_kwh=round(kwh, 2), source="live_reading", fetched_at=datetime.fromisoformat(timestamp_str))


def load_model(conn, day_hours, night_hours):
    """
    Provider: expected house energy consumption (kWh) for a given split of
    day/night hours. Wraps the existing (already cached) get_expected_load(),
    converting its day/night watt rates into a single kWh figure for
    whatever window the caller needs - a 7-day cycle (Layer 1), the
    remaining hours until sunset (Layer 2), or until next sunrise (relax).
    """
    load_estimate = get_expected_load(conn)

    day_kwh = (load_estimate["day_load_w"] * day_hours) / 1000
    night_kwh = (load_estimate["night_load_w"] * night_hours) / 1000
    total_kwh = day_kwh + night_kwh

    source = f"day:{load_estimate['day_source']},night:{load_estimate['night_source']}"

    return ProviderResult(
        value_kwh=round(total_kwh, 2),
        source=source,
        fetched_at=datetime.now(),
        failed=False  # get_expected_load always returns a usable estimate (falls back to config seasonal defaults), never fails outright
    )
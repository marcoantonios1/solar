from dataclasses import dataclass
from datetime import datetime

from config_loader import config

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
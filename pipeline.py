import pandas as pd
from solar_model import get_sun_times_for_date
from providers import solar_forecast, load_model, battery_state
from output_mode_manager import classify_energy_balance
from proposal import Proposal


def split_day_night_hours(start, end):
    """
    Given an arbitrary [start, end] window, returns (day_hours, night_hours)
    - how much of that window falls during daylight vs. night, using real
    sunrise/sunset per date (not a fixed hour boundary). Handles windows
    that span multiple days/nights correctly.
    """
    day_hours = 0.0
    night_hours = 0.0
    current = start

    while current < end:
        date_str = current.strftime("%Y-%m-%d")
        sunrise, sunset = get_sun_times_for_date(date_str)
        next_midnight = (current + pd.Timedelta(days=1)).normalize()
        segment_end = min(end, next_midnight)

        overlap_start = max(current, sunrise)
        overlap_end = min(segment_end, sunset)
        if overlap_end > overlap_start:
            day_hours += (overlap_end - overlap_start).total_seconds() / 3600

        total_segment_hours = (segment_end - current).total_seconds() / 3600
        day_in_segment = max(0, (overlap_end - overlap_start).total_seconds() / 3600) if overlap_end > overlap_start else 0
        night_hours += total_segment_hours - day_in_segment

        current = segment_end

    return round(day_hours, 2), round(night_hours, 2)


def run_pipeline(conn, start, end, source):
    """
    THE shared pipeline: provider -> balance -> policy, for any [start, end]
    window. Layer 1 calls this once per sunrise-to-sunrise cycle (7 times);
    Layer 2 calls it once (now -> sunset); the relax check calls it once
    (now -> next sunrise). Returns a Proposal, or None if a provider failed
    (making forecast-fetch-failure bugs structurally impossible, rather than
    a patched-over edge case).
    """
    solar_result = solar_forecast(start, end)
    if solar_result.failed:
        return None

    day_hours, night_hours = split_day_night_hours(start, end)
    house_result = load_model(conn, day_hours, night_hours)
    if house_result.failed:
        return None

    battery_result = battery_state(conn)
    if battery_result.failed:
        return None

    charger_mode, output_priority, label, balance_kwh = classify_energy_balance(
        solar_expected_kwh=solar_result.value_kwh,
        battery_available_kwh=battery_result.value_kwh,
        house_expected_kwh=house_result.value_kwh,
    )

    return Proposal(
        charger_mode=charger_mode,
        output_priority=output_priority,
        reason=label,
        source=source
    )
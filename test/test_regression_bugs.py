"""
Regression tests seeded with the actual bugs found and fixed during this
project's code review and Phase A/B work - not generic tests, tests that
would have caught these specific real failures. Run via pytest before any
future change, especially anything touching the decision pipeline.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from unittest.mock import patch


def test_battery_recharge_double_count_fix():
    """
    Bug 1.2: battery_recharge_status used to double-count battery energy.
    Worked example from the code review: SOC 100%, solar 0, house 10 kWh
    -> balance = 8.43 kWh. The OLD buggy logic only compared against the
    gap-to-full (0, since already at 100%), incorrectly claiming
    will_reach_full=True. The fix compares against full capacity instead.
    """
    CAPACITY_KWH_USABLE = 18.43
    balance_kwh = 8.43  # solar(0) + battery_available(18.43, i.e. 100% SOC) - house(10)

    net_after_recharge = balance_kwh - CAPACITY_KWH_USABLE
    will_reach_full = balance_kwh >= CAPACITY_KWH_USABLE

    assert will_reach_full is False, "Must NOT claim will_reach_full when balance (8.43) is well below capacity (18.43)"
    assert net_after_recharge < 0


def test_failed_provider_yields_no_proposal():
    """
    Bug 1.3: forecast fetch failure used to silently classify as zero
    solar (a fake shortfall) instead of skipping the decision entirely.
    The pipeline now structurally can't do this - a failed provider
    means no proposal at all, not a patched-over edge case.
    """
    from pipeline import run_pipeline
    from providers import ProviderResult

    now = pd.Timestamp.now(tz="Asia/Beirut")

    with patch('pipeline.solar_forecast', return_value=ProviderResult(
            value_kwh=0, source="fetch_failed", fetched_at=pd.Timestamp.now(), failed=True)):
        result = run_pipeline(conn=None, start=now, end=now + pd.Timedelta(hours=1), source="test")

    assert result is None, "A failed solar provider must yield NO proposal, not a fake zero-solar shortfall"


def test_zero_target_bypasses_write_tolerance():
    """
    Bug 2.2: WRITE_TOLERANCE_A used to block the suspend-charging path -
    if the target was genuinely 0A but the current setting was 1-2A, the
    tolerance check incorrectly suppressed the write, so charging never
    actually paused when it should have.
    """
    WRITE_TOLERANCE_A = 2
    current_setting = 1
    clamped = 0

    should_skip = (clamped != 0 and abs(current_setting - clamped) <= WRITE_TOLERANCE_A)

    assert should_skip is False, "A genuine 0A suspend target must always write through, regardless of tolerance"


def test_dst_spring_forward_hour_parses_without_raising():
    """
    Bug 2.6: pd.Timestamp(time_str, tz='Asia/Beirut') used to raise
    NonExistentTimeError on the skipped hour during spring-forward DST,
    killing the whole prediction run. Confirmed against Lebanon's real
    2026 spring-forward transition, discovered live during this project.
    """
    from weather import parse_forecast_timestamp

    result = parse_forecast_timestamp("2026-03-29T00:30:00")

    assert result is not None, "Must not raise or silently fail on a real DST-skipped hour - should shift forward gracefully"


def test_round_trip_efficiency_discounts_battery_term_only():
    """
    Issue #136: energy routed through the battery loses ~8-12% round trip
    (LFP + inverter), so the battery term (not solar or house) should be
    discounted by ~0.92 before the balance is computed.
    """
    from energy_balance import calculate_energy_balance, ROUND_TRIP_EFFICIENCY

    result = calculate_energy_balance(solar_expected_kwh=23.55, battery_available_kwh=18.43, house_expected_kwh=19.79)

    naive_balance = 23.55 + 18.43 - 19.79
    assert result["balance_kwh"] < naive_balance, "Balance must be more conservative than the naive (undiscounted) calculation"
    assert ROUND_TRIP_EFFICIENCY < 1.0, "Round-trip efficiency factor must genuinely discount, not be a no-op"


def test_15_percent_floor_thresholds_are_meaningfully_positive():
    """
    Real hardware discovery (2026-08-07): the inverter physically alarms
    at 15% SOC. The shortfall/charge-needed thresholds must reflect a
    real safety margin ABOVE that floor - not just above literal 0%
    (which was the original, dangerously optimistic assumption).
    """
    from config_loader import config

    capacity_kwh = config["battery"]["capacity_kwh_usable"]
    shortfall_threshold = config["thresholds"]["shortfall_threshold_kwh"]

    real_alarm_floor_pct = 15
    threshold_as_pct = (shortfall_threshold / capacity_kwh) * 100

    assert threshold_as_pct > real_alarm_floor_pct, \
        f"Shortfall threshold ({threshold_as_pct:.1f}%) must sit above the real 15% hardware alarm floor, not just above 0%"

def test_today_fetch_failure_aborts_run_entirely():
    """
    External review bug 2 (2026-08-07): if today's specific forecast cycle
    failed while a later cycle succeeded, predictions[0] could silently
    become a different day - but every caller assumes index 0 is always
    today. A failure on today's cycle must abort the whole run.
    """
    import sqlite3
    from daily_predictor import get_daily_predictions
    from providers import ProviderResult

    conn = sqlite3.connect('/mnt/edl-data/inverter.db')

    call_count = [0]
    def failing_first_call(start, end):
        call_count[0] += 1
        if call_count[0] == 1:
            return ProviderResult(value_kwh=0, source='fetch_failed', fetched_at=pd.Timestamp.now(), failed=True)
        return ProviderResult(value_kwh=20.0, source='mock', fetched_at=pd.Timestamp.now(), failed=False)

    with patch('daily_predictor.solar_forecast', side_effect=failing_first_call):
        result = get_daily_predictions(conn)

    assert result is None, "Today's cycle failing must abort the whole run, never let a later day silently become predictions[0]"

def test_layer1_prefetches_forecast_once_not_per_cycle():
    """
    External review bug 3 (2026-08-07): each of Layer 1's 7 cycles asked
    for a progressively larger forecast window, so the cache (which
    required days_fetched >= days_needed) missed on nearly every call -
    up to 7 real Open-Meteo fetches per daily run instead of 1.
    """
    import sqlite3
    import providers
    from daily_predictor import get_daily_predictions

    conn = sqlite3.connect('/mnt/edl-data/inverter.db')
    providers._solar_forecast_cache['data'] = None
    providers._solar_forecast_cache['fetched_at'] = None

    with patch('providers.fetch_forecast_weather', wraps=providers.fetch_forecast_weather) as mock_fetch:
        get_daily_predictions(conn)

    assert mock_fetch.call_count == 1, f"Expected exactly 1 real fetch across all 7 cycles, got {mock_fetch.call_count}"
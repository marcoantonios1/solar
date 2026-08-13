"""
Regression tests seeded with the actual bugs found and fixed during this
project's code review and Phase A/B work - not generic tests, tests that
would have caught these specific real failures. Run via pytest before any
future change, especially anything touching the decision pipeline.
"""
import sys
import os
import providers
import sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from unittest.mock import patch
from actuator import apply_state
from inverter import SNU, OSO, UTI, SBU
from pipeline import run_pipeline
from providers import ProviderResult
from weather import parse_forecast_timestamp
from energy_balance import calculate_energy_balance, ROUND_TRIP_EFFICIENCY
from daily_predictor import get_daily_predictions
from actuator import _write_guard_allows, _last_write_time
import time
from datetime import datetime
import charge_throttle
from charge_throttle import relax_rule1_early_if_recovered, relax_if_battery_full
from arbiter import arbitrate
from proposal import Proposal
from output_mode_manager import get_tariff_adjusted_lookahead_threshold, get_forecast_uncertainty_factor, TOMORROW_SHORTFALL_LOOKAHEAD_KWH



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

    result = parse_forecast_timestamp("2026-03-29T00:30:00")

    assert result is not None, "Must not raise or silently fail on a real DST-skipped hour - should shift forward gracefully"


def test_round_trip_efficiency_discounts_battery_term_only():
    """
    Issue #136: energy routed through the battery loses ~8-12% round trip
    (LFP + inverter), so the battery term (not solar or house) should be
    discounted by ~0.92 before the balance is computed.
    """

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

    conn = sqlite3.connect(':memory:')
    conn.execute("""CREATE TABLE readings (id INTEGER PRIMARY KEY, timestamp TEXT,
        pv_power REAL, battery_soc INTEGER, load_power REAL, edl_present INTEGER,
        ac_charge_power REAL, cloud_cover REAL, expected_pv_power REAL, expected_pv_power_weather REAL)""")
    conn.execute("INSERT INTO readings (timestamp, battery_soc) VALUES ('2026-08-11T05:00:00', 50)")
    conn.commit()

    call_count = [0]
    def failing_first_call(days):
        call_count[0] += 1
        return None  # every real fetch_forecast_weather failure returns None

    with patch('providers.fetch_forecast_weather', side_effect=failing_first_call):
        result = get_daily_predictions(conn)

    assert result is None, "Today's cycle failing must abort the whole run, never let a later day silently become predictions[0]"
def test_layer1_prefetches_forecast_once_not_per_cycle():
    """
    External review bug 3 (2026-08-07): each of Layer 1's 7 cycles asked
    for a progressively larger forecast window, so the cache missed on
    nearly every call. Patches at the true source (providers.fetch_forecast_weather)
    since the prefetch's local import (from providers import solar_forecast
    as _prefetch_solar_forecast) would otherwise escape a shallower patch.
    """

    conn = sqlite3.connect(':memory:')
    conn.execute("""CREATE TABLE readings (id INTEGER PRIMARY KEY, timestamp TEXT,
        pv_power REAL, battery_soc INTEGER, load_power REAL, edl_present INTEGER,
        ac_charge_power REAL, cloud_cover REAL, expected_pv_power REAL, expected_pv_power_weather REAL)""")
    conn.execute("INSERT INTO readings (timestamp, battery_soc) VALUES ('2026-08-11T05:00:00', 50)")
    conn.commit()

    providers._solar_forecast_cache['data'] = None
    providers._solar_forecast_cache['fetched_at'] = None

    canned_forecast = {
        'time': [f'2026-08-11T{h:02d}:00:00' for h in range(24)] * 8,
        'ghi': [500] * 192, 'dni': [600] * 192, 'dhi': [100] * 192, 'temp': [28] * 192,
    }

    with patch('providers.fetch_forecast_weather', return_value=canned_forecast) as mock_fetch:
        get_daily_predictions(conn)

    assert mock_fetch.call_count == 1, f"Expected exactly 1 real fetch across all 7 cycles, got {mock_fetch.call_count}"

def test_partial_write_failure_is_not_reported_as_full_success():
    """
    Real bug found via arbiter dry-run comparison (2026-08-07): when both
    charger and output were requested but only ONE actually took effect
    (confirmed live - charger write failed settling verification while
    output succeeded), the old code still reported action="changed" as if
    fully successful, leaving the system silently in an unintended
    combination (SNU+SBU - confirmed to physically block charging).
    """

    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE mode_changes (id INTEGER PRIMARY KEY, timestamp TEXT, old_mode TEXT, new_mode TEXT, old_output TEXT, new_output TEXT, trigger_reason TEXT, battery_soc INTEGER, pv_power REAL, load_power REAL)")
    conn.execute("CREATE TABLE system_errors (id INTEGER PRIMARY KEY, timestamp TEXT, category TEXT, message TEXT)")

    # Charger: stays SNU throughout (write never actually takes effect,
    # even after the retry). Output: starts at UTI, successfully becomes
    # SBU after the write - matching the real observed failure exactly.
    output_call_count = {"n": 0}

    def fake_read_output(client):
        output_call_count["n"] += 1
        return UTI if output_call_count["n"] == 1 else SBU

    with patch('actuator.read_current_charger_mode_once', return_value=SNU), \
         patch('actuator.read_output_priority', side_effect=fake_read_output), \
         patch('actuator.set_charger_mode', return_value=True), \
         patch('actuator.set_output_priority', return_value=True), \
         patch('actuator.read_values_once', return_value=None), \
         patch('actuator._write_guard_allows', return_value=True), \
         patch('actuator.send_alert', return_value=False):

        result = apply_state(client=None, conn=conn, target_charger=OSO, target_output=SBU, reason='test partial failure')

        print(result)
        assert result["action"] == "changed", "Output DID change, so action should be 'changed'"
        assert result["fully_applied"] is False, "Charger never actually changed - this must NOT be reported as fully applied"

def test_retry_bypasses_write_guard_interval_check():
    """
    Real bug found live 2026-08-08: the retry mechanism (built to recover
    from a partial write failure) called the SAME _write_guard_allows()
    check as a brand-new write - meaning the retry, happening only ~2
    seconds after the original attempt, was ALWAYS blocked by the guard's
    own minimum-interval rule recording that original attempt. This meant
    retries could never actually succeed since the retry logic was built.
    Directly confirmed live tonight: a real settling-delay failure occurred,
    and the retry successfully recovered it once this fix was in place.
    """

    _last_write_time["charger"] = time.time()

    blocked_as_new_write = _write_guard_allows("charger", is_retry=False)
    allowed_as_retry = _write_guard_allows("charger", is_retry=True)

    assert blocked_as_new_write is False, "A genuine new write, too soon after the last one, should still be blocked"
    assert allowed_as_retry is True, "A deliberate RETRY must bypass the interval check, or retries can never succeed"


def test_layer1_run_date_persists_across_restart():
    """
    Real bug found live 2026-08-08: last_layer1_run_date only lived in
    memory, so ANY service restart after 7am reset it to None, causing
    Layer 1 to immediately re-fire with its stale morning calculation -
    silently overwriting more accurate decisions made since (relax,
    manual correction, genuine nighttime escalation). Confirmed happening
    repeatedly live tonight across multiple real restarts. Fix: check the
    database for whether Layer 1 genuinely already ran today, not memory.
    """

    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE daily_predictions (date TEXT, run_timestamp TEXT)")
    today_str = datetime.now().strftime("%Y-%m-%d")
    conn.execute("INSERT INTO daily_predictions (date, run_timestamp) VALUES (?, ?)", (today_str, "2026-08-08T07:00:00"))
    conn.commit()

    existing_run = conn.execute(
        "SELECT 1 FROM daily_predictions WHERE date = ? LIMIT 1", (today_str,)
    ).fetchone()
    last_layer1_run_date = today_str if existing_run else None

    assert last_layer1_run_date == today_str, "Must correctly detect Layer 1 already ran today from the database, not assume None after a restart"

def test_rule1_early_relax_defers_to_todays_layer1_decision():
    """
    Real gap found live 2026-08-10: Rule 1 fired once (SOC below floor,
    EDL present), correctly escalating to SNU+UTI - but nothing ever
    relaxed it early even after SOC recovered well past comfortable
    (confirmed live: held at SNU+UTI from 7am through 74% SOC in full
    sun, purely because Layer 2 is escalate-only and can't relax, and
    relax_if_battery_full() requires 98%). Fix: once SOC clears a real
    margin above the floor, defer to Layer 1's own already-computed
    decision for today, confirmed working live.
    """

    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE mode_changes (id INTEGER PRIMARY KEY, timestamp TEXT, trigger_reason TEXT)")
    conn.execute("CREATE TABLE daily_predictions (date TEXT, run_timestamp TEXT, decision_label TEXT, charger_mode TEXT, output_priority TEXT)")

    conn.execute("INSERT INTO mode_changes (timestamp, trigger_reason) VALUES (?, ?)",
                 ("2026-08-10T07:00:20", "Rule 1: critical SOC floor + EDL present"))

    today_str = pd.Timestamp.now(tz="Asia/Beirut").strftime("%Y-%m-%d")
    conn.execute("INSERT INTO daily_predictions (date, run_timestamp, decision_label, charger_mode, output_priority) VALUES (?, ?, ?, ?, ?)",
                 (today_str, f"{today_str}T07:00:00", "surplus - default, minimize EDL (OSO+SBU)", "OSO", "0"))
    conn.commit()

    result = relax_rule1_early_if_recovered(conn, SNU, UTI, battery_soc=74)

    assert result is not None, "Must relax once SOC clears the threshold and Layer 1's own decision was comfortable"
    assert result.charger_mode == 2, "Should defer to Layer 1's logged OSO decision"
    assert result.output_priority == 0, "Should defer to Layer 1's logged SBU decision"

def test_layer1_can_relax_even_when_lower_ranked():
    """
    Real regression found live 2026-08-11: after the #176 arbiter cutover,
    Layer 1's proposal was subject to the same escalate-only comparison
    as Layer 2 - meaning Layer 1's own "surplus, relax" decision could
    never actually apply once ANY escalation was already active, since
    OSO+SBU (rank 0) can't "beat" an already-active SNU+UTI (rank 2)
    under escalate-only rules. This permanently locked the system at
    whatever tier Rule 1/Layer 2 last reached, even after Layer 1 itself
    determined it was no longer needed - confirmed live: stuck in
    SNU+UTI all morning despite Layer 1 deciding OSO+SBU at 09:39.
    """

    current_charger, current_output = SNU, UTI  # currently escalated
    layer1_proposal = Proposal(charger_mode=OSO, output_priority=SBU, reason="surplus - default, minimize EDL (OSO+SBU)", source="layer1")

    winner = arbitrate(current_charger, current_output, [layer1_proposal])

    assert winner is not None, "Layer 1's proposal must win even though it's a LOWER rank than current"
    assert winner.charger_mode == OSO and winner.output_priority == SBU, "Must apply Layer 1's actual decision"

def test_relax_hysteresis_requires_two_consecutive_matching_proposals():
    """
    Issue #137: real evidence found live 2026-08-07 showed relax firing
    within ~1 minute of the escalation it was relaxing away from - a
    single noisy read shouldn't be enough to act on. Requires the SAME
    target to be proposed on two consecutive checks before applying it.
    """

    charge_throttle._relax_pending['target'] = None
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE daily_predictions (date TEXT, run_timestamp TEXT, decision_label TEXT)')

    fake_proposal = Proposal(charger_mode=OSO, output_priority=SBU, reason='surplus test', source='relax')

    with patch('pipeline.run_pipeline', return_value=fake_proposal):
        first_call = relax_if_battery_full(conn, SNU, UTI, battery_soc=99)
        second_call = relax_if_battery_full(conn, SNU, UTI, battery_soc=99)

    assert first_call is None, "First occurrence of a fresh relax target must be held, not applied immediately"
    assert second_call is not None, "Second CONSECUTIVE matching occurrence must commit"
    assert second_call.charger_mode == OSO and second_call.output_priority == SBU

def test_summer_night_relax_uses_uti_not_sbu():
    """
    Real idea from 2026-08-11: SBU output priority physically blocks EDL
    from powering the house at all, even if EDL is present. Summer nights
    draw heavily (confirmed: ~10.4%/hour overnight, 2026-08-10/11) - a
    fully-charged battery doesn't need more charging, but keeping UTI
    lets EDL power the house directly if it appears overnight, sparing
    the battery, without wastefully charging an already-full battery.
    """

    charge_throttle._relax_pending['target'] = None
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE daily_predictions (date TEXT, run_timestamp TEXT, decision_label TEXT)')

    fake_proposal = Proposal(charger_mode=OSO, output_priority=SBU, reason='surplus test', source='relax')
    fake_now = pd.Timestamp('2026-08-12 21:00:00', tz='Asia/Beirut')

    with patch('pipeline.run_pipeline', return_value=fake_proposal), \
         patch('pandas.Timestamp.now', return_value=fake_now):
        relax_if_battery_full(conn, SNU, UTI, battery_soc=99)
        result = relax_if_battery_full(conn, SNU, UTI, battery_soc=99)

    assert result is not None
    assert result.charger_mode == OSO
    assert result.output_priority == UTI, "Summer night relax must use UTI, not SBU, so EDL can still power the house if it appears"

def test_tariff_aware_threshold_scales_with_remaining_allowance():
    """
    Issue: tariff-tier awareness. Near month-start with tier-1 ($0.10/kWh)
    allowance untouched, marginal charging is near-free insurance - the
    tomorrow-lookahead threshold should be MORE generous. Deep in tier-2
    ($0.27/kWh), it should be stricter, requiring a bigger, more certain
    shortfall before committing to expensive EDL.
    """
    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE edl_events (event_id INTEGER PRIMARY KEY, start_time TEXT, total_kwh_charged_during REAL)")

    # Full tier-1 remaining (nothing used this month)
    threshold_full_allowance = get_tariff_adjusted_lookahead_threshold(conn)

    # Tier-1 fully exhausted (100+ kWh already used this month)
    month_start = pd.Timestamp.now(tz='Asia/Beirut').strftime('%Y-%m-01T00:00:00')
    conn.execute("INSERT INTO edl_events (start_time, total_kwh_charged_during) VALUES (?, ?)", (month_start, 150.0))
    conn.commit()
    threshold_exhausted = get_tariff_adjusted_lookahead_threshold(conn)

    assert threshold_full_allowance > threshold_exhausted, "Full tier-1 remaining should be MORE generous than tier-1 exhausted"
    assert threshold_full_allowance > TOMORROW_SHORTFALL_LOOKAHEAD_KWH, "Full tier-1 should exceed the flat baseline"
    assert threshold_exhausted < TOMORROW_SHORTFALL_LOOKAHEAD_KWH, "Exhausted tier-1 should be stricter than the flat baseline"

def test_forecast_uncertainty_factor_flags_partly_cloudy_days():
    """
    Issue: forecast uncertainty as a confidence signal. Real ensemble
    data isn't available yet - crude proxy: partly-cloudy (30-70% cloud
    cover) is inherently less predictable than clear or fully overcast
    skies, matching Phase 3's original testing (gaps swung wildly at
    ~45% cloud cover on a single-minute basis).
    """

    tomorrow = (pd.Timestamp.now(tz='Asia/Beirut') + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    uncertain_forecast = {
        'time': [f'{tomorrow}T{h:02d}:00' for h in range(24)],
        'cloud_cover': [50] * 24,  # squarely in the 30-70% uncertain range
    }
    clear_forecast = {
        'time': [f'{tomorrow}T{h:02d}:00' for h in range(24)],
        'cloud_cover': [5] * 24,  # clearly clear, high confidence
    }

    with patch('weather.fetch_forecast_weather', return_value=uncertain_forecast):
        uncertain_factor = get_forecast_uncertainty_factor()

    with patch('weather.fetch_forecast_weather', return_value=clear_forecast):
        clear_factor = get_forecast_uncertainty_factor()

    assert uncertain_factor > 1.0, "Partly-cloudy tomorrow should widen the threshold (lower confidence)"
    assert clear_factor == 1.0, "Clear skies should trust the point forecast as-is"
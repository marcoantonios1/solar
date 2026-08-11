import pandas as pd
from config_loader import config
from inverter import SNU, UTI, OSO, SBU, read_max_charge_current, set_max_charge_current
from breaker_safety import calculate_safe_charge_current
from proposal import Proposal
from near_term_decision import get_tier_rank

BATTERY_MAX_CHARGE_A = config["battery"]["max_charge_current_a"]
MIN_CHARGE_A = config["breaker_safety"]["min_charge_current_a"]
MAX_REGISTER_CHARGE_CURRENT_A = config["breaker_safety"]["max_register_charge_current_a"]
WRITE_TOLERANCE_A = config["breaker_safety"]["write_tolerance_a"]  # only rewrite if the new value differs by more than this
FULL_SOC_RELAX_THRESHOLD = config["thresholds"]["full_soc_relax_threshold"]
RULE1_EARLY_RELAX_SOC_THRESHOLD = config["thresholds"]["rule1_early_relax_soc_threshold"]

_CHARGER_NAME_TO_VALUE = {"CSO": 0, "SNU": 1, "OSO": 2}


def relax_if_battery_full(conn, current_charger_mode, current_output_priority, battery_soc):
    """
    Pure function, now built on the shared pipeline (Issue #150) instead of
    its own separate calculation. Everything else about its behavior -
    the SOC gate, escalate-only comparison, tomorrow-lookahead preservation
    - stays identical to before.
    """
    if battery_soc < FULL_SOC_RELAX_THRESHOLD:
        return None

    from near_term_decision import get_tier_rank
    from pipeline import run_pipeline
    from solar_model import get_sun_times_for_date

    now = pd.Timestamp.now(tz="Asia/Beirut")
    sunrise_today, sunset_today = get_sun_times_for_date(now.strftime("%Y-%m-%d"))

    if sunrise_today <= now <= sunset_today:
        next_sunrise, _ = get_sun_times_for_date((now + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    else:
        if now < sunrise_today:
            next_sunrise = sunrise_today
        else:
            next_sunrise, _ = get_sun_times_for_date((now + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))

    fresh_proposal = run_pipeline(conn, now, next_sunrise, source="relax")
    if fresh_proposal is None:
        return None

    current_rank = get_tier_rank(current_charger_mode, current_output_priority)
    fresh_rank = get_tier_rank(fresh_proposal.charger_mode, fresh_proposal.output_priority)

    if fresh_rank >= current_rank:
        return None

    today_str = now.strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT decision_label FROM daily_predictions WHERE date = ? ORDER BY run_timestamp DESC LIMIT 1",
        (today_str,)
    ).fetchone()
    preserving_for_tomorrow = row is not None and row[0] is not None and "tomorrow predicted shortfall" in row[0]

    target_charger = fresh_proposal.charger_mode
    target_output = UTI if (preserving_for_tomorrow and target_charger == OSO) else fresh_proposal.output_priority

    if current_charger_mode == target_charger and current_output_priority == target_output:
        return None

    reason = f"Relax (battery full): {fresh_proposal.reason}"
    if preserving_for_tomorrow:
        reason += " - preserving buffer for predicted cloudy tomorrow"

    return Proposal(charger_mode=target_charger, output_priority=target_output, reason=reason, source="relax")


def relax_rule1_early_if_recovered(conn, current_charger_mode, current_output_priority, battery_soc):
    """
    Rule 1 (Layer 3) deliberately stays "dumb" - always fires below the
    critical SOC floor, unconditionally, no forecast dependency. But once
    it HAS fired and SOC genuinely recovers, nothing previously relaxed it
    early - it stayed escalated all the way to 98% or tomorrow's reset,
    even hours after the emergency had clearly passed (found live,
    2026-08-10: SNU+UTI held from 7am through 72% SOC in full sun).

    Fix: once SOC recovers to a real margin above the floor, defer back
    to whatever LAYER 1 already decided for today (queried from its
    logged decision). Determines whether the ACTIVE escalation came from
    Rule 1 by checking the most recent mode_changes entry directly from
    the database - not an in-memory flag, so this correctly survives
    restarts (same class of bug just fixed for last_layer1_run_date).
    """
    if battery_soc < RULE1_EARLY_RELAX_SOC_THRESHOLD:
        return None

    last_change = conn.execute(
        "SELECT trigger_reason FROM mode_changes ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()

    if last_change is None or not last_change[0].startswith("Rule 1:"):
        return None

    today_str = pd.Timestamp.now(tz="Asia/Beirut").strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT charger_mode, output_priority, decision_label FROM daily_predictions WHERE date = ? ORDER BY run_timestamp DESC LIMIT 1",
        (today_str,)
    ).fetchone()

    if row is None:
        return None

    charger_name, output_str, label = row
    if charger_name not in _CHARGER_NAME_TO_VALUE:
        return None

    target_charger = _CHARGER_NAME_TO_VALUE[charger_name]
    target_output = int(output_str)

    current_rank = get_tier_rank(current_charger_mode, current_output_priority)
    target_rank = get_tier_rank(target_charger, target_output)

    if target_rank >= current_rank:
        return None

    if current_charger_mode == target_charger and current_output_priority == target_output:
        return None

    reason = f"Rule 1 early relax (SOC recovered to {battery_soc}%) - reverting to today's Layer 1 decision: {label}"
    return Proposal(charger_mode=target_charger, output_priority=target_output, reason=reason, source="relax")


def adjust_charge_current_if_needed(client, current_charger_mode, current_output_priority, load_power_w, pv_power_w):
    """
    If currently in SNU+UTI (EDL charging + powering house simultaneously),
    recalculates the safe DC charge current given live load, and writes it
    if meaningfully different from the current setting. Does nothing if not
    in that state - no throttling needed when EDL isn't handling both jobs
    at once.
    """
    if not (current_charger_mode == SNU and current_output_priority == UTI):
        return None

    safe_current = calculate_safe_charge_current(load_power_w, pv_power_w)

    if safe_current < MIN_CHARGE_A:
        # Load leaves no safe room for even the minimum charge current -
        # suspend charging entirely rather than forcing a floor that could
        # violate the breaker safety margin. House still gets powered via
        # UTI; charging just pauses until load drops.
        clamped = 0
    else:
        clamped = min(safe_current, BATTERY_MAX_CHARGE_A, MAX_REGISTER_CHARGE_CURRENT_A)

    current_setting = read_max_charge_current(client)
    if current_setting is None:
        return None

    if clamped != 0 and abs(current_setting - clamped) <= WRITE_TOLERANCE_A:
        return {"action": "no_change", "current": current_setting, "target": clamped}

    set_max_charge_current(client, clamped)
    return {"action": "adjusted", "from": current_setting, "to": clamped}
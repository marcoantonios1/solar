import pandas as pd
from config_loader import config
from inverter import SNU, UTI, OSO, SBU, read_max_charge_current, set_max_charge_current
from breaker_safety import calculate_safe_charge_current
from proposal import Proposal

BATTERY_MAX_CHARGE_A = config["battery"]["max_charge_current_a"]
MIN_CHARGE_A = 10
WRITE_TOLERANCE_A = 2  # only rewrite if the new value differs by more than this

FULL_SOC_RELAX_THRESHOLD = config["thresholds"]["full_soc_relax_threshold"]


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
        clamped = min(safe_current, BATTERY_MAX_CHARGE_A, 130)

    current_setting = read_max_charge_current(client)
    if current_setting is None:
        return None

    if clamped != 0 and abs(current_setting - clamped) <= WRITE_TOLERANCE_A:
        return {"action": "no_change", "current": current_setting, "target": clamped}

    set_max_charge_current(client, clamped)
    return {"action": "adjusted", "from": current_setting, "to": clamped}
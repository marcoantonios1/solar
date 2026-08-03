import pandas as pd
from config_loader import config
from inverter import SNU, UTI, OSO, SBU, read_max_charge_current, set_max_charge_current, set_charger_mode, set_output_priority
from breaker_safety import calculate_safe_charge_current

BATTERY_MAX_CHARGE_A = config["battery"]["max_charge_current_a"]
MIN_CHARGE_A = 10
WRITE_TOLERANCE_A = 2  # only rewrite if the new value differs by more than this

FULL_SOC_RELAX_THRESHOLD = config["thresholds"]["full_soc_relax_threshold"]


def relax_if_battery_full(client, conn, current_charger_mode, current_output_priority, battery_soc):
    """
    Live re-check using the shared energy-balance calculation, not a flat
    SOC threshold - relaxes down to whatever tier the current live numbers
    actually justify (right now, until next sunrise), rather than always
    charging toward ~98-100%. Preserves the "buffer for predicted cloudy
    tomorrow" check on top, same as before.
    """
    from near_term_check import get_live_projection_until_sunrise
    from near_term_decision import get_tier_rank

    projection = get_live_projection_until_sunrise(conn)
    if projection is None:
        return None

    current_rank = get_tier_rank(current_charger_mode, current_output_priority)
    fresh_rank = get_tier_rank(projection["charger_mode"], projection["output_priority"])

    if fresh_rank >= current_rank:
        return None  # nothing to relax - current state is already justified or under-escalated

    today_str = pd.Timestamp.now(tz="Asia/Beirut").strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT decision_label FROM daily_predictions WHERE date = ? ORDER BY run_timestamp DESC LIMIT 1",
        (today_str,)
    ).fetchone()
    preserving_for_tomorrow = row is not None and row[0] is not None and "tomorrow predicted shortfall" in row[0]

    target_charger = projection["charger_mode"]
    target_output = UTI if (preserving_for_tomorrow and target_charger == OSO) else projection["output_priority"]

    if current_charger_mode == target_charger and current_output_priority == target_output:
        return None

    set_charger_mode(client, target_charger)
    set_output_priority(client, target_output)
    return {
        "action": "relaxed",
        "battery_soc": battery_soc,
        "new_tier": projection["label"],
        "preserving_for_tomorrow": preserving_for_tomorrow,
    }


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

    if abs(current_setting - clamped) <= WRITE_TOLERANCE_A:
        return {"action": "no_change", "current": current_setting, "target": clamped}

    set_max_charge_current(client, clamped)
    return {"action": "adjusted", "from": current_setting, "to": clamped}
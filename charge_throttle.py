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
    If the battery is genuinely full (live SOC, not a forecast), stop
    charging (no more benefit) - but check WHY today's escalation happened
    before deciding how far to relax. If Layer 1 escalated specifically
    because tomorrow looks bad (proactively building buffer), keep UTI so
    EDL keeps powering the house directly if present, preserving that
    buffer rather than draining it via SBU (which draws from battery
    before EDL). Otherwise, fully relax to OSO+SBU.
    """
    if battery_soc < FULL_SOC_RELAX_THRESHOLD:
        return None

    today_str = pd.Timestamp.now(tz="Asia/Beirut").strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT decision_label FROM daily_predictions WHERE date = ? ORDER BY run_timestamp DESC LIMIT 1",
        (today_str,)
    ).fetchone()

    preserving_for_tomorrow = row is not None and row[0] is not None and "tomorrow predicted shortfall" in row[0]

    target_charger = OSO
    target_output = UTI if preserving_for_tomorrow else SBU

    if current_charger_mode == target_charger and current_output_priority == target_output:
        return None

    set_charger_mode(client, target_charger)
    set_output_priority(client, target_output)
    return {
        "action": "relaxed_full",
        "battery_soc": battery_soc,
        "preserving_for_tomorrow": preserving_for_tomorrow,
        "target_output": target_output,
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
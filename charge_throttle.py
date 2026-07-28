from config_loader import config
from inverter import SNU, UTI, OSO, SBU, read_max_charge_current, set_max_charge_current, set_charger_mode, set_output_priority
from breaker_safety import calculate_safe_charge_current

BATTERY_MAX_CHARGE_A = config["battery"]["max_charge_current_a"]
MIN_CHARGE_A = 10
WRITE_TOLERANCE_A = 2  # only rewrite if the new value differs by more than this

FULL_SOC_RELAX_THRESHOLD = config["thresholds"]["full_soc_relax_threshold"]


def relax_if_battery_full(client, current_charger_mode, current_output_priority, battery_soc):
    """
    If the battery is genuinely full (live SOC, not a forecast), relax back
    to OSO+SBU regardless of what Layer 1/2 decided earlier - there's no
    charging benefit left to gain, only unnecessary EDL cost to avoid by
    continuing to power the house from EDL instead of the now-full battery.
    """
    if battery_soc < FULL_SOC_RELAX_THRESHOLD:
        return None

    if current_charger_mode == OSO and current_output_priority == SBU:
        return None  # already relaxed, nothing to do

    set_charger_mode(client, OSO)
    set_output_priority(client, SBU)
    return {"action": "relaxed_full", "battery_soc": battery_soc}


def adjust_charge_current_if_needed(client, current_charger_mode, current_output_priority, load_power_w):
    """
    If currently in SNU+UTI (EDL charging + powering house simultaneously),
    recalculates the safe DC charge current given live load, and writes it
    if meaningfully different from the current setting. Does nothing if not
    in that state - no throttling needed when EDL isn't handling both jobs
    at once.
    """
    if not (current_charger_mode == SNU and current_output_priority == UTI):
        return None

    safe_current = calculate_safe_charge_current(load_power_w)

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
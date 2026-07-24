from config_loader import config
from inverter import SNU, UTI, read_max_charge_current, set_max_charge_current
from breaker_safety import calculate_safe_charge_current

BATTERY_MAX_CHARGE_A = config["battery"]["max_charge_current_a"]
MIN_CHARGE_A = 10
WRITE_TOLERANCE_A = 2  # only rewrite if the new value differs by more than this


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
    clamped = max(MIN_CHARGE_A, min(safe_current, BATTERY_MAX_CHARGE_A, 130))

    current_setting = read_max_charge_current(client)
    if current_setting is None:
        return None

    if abs(current_setting - clamped) <= WRITE_TOLERANCE_A:
        return {"action": "no_change", "current": current_setting, "target": clamped}

    set_max_charge_current(client, clamped)
    return {"action": "adjusted", "from": current_setting, "to": clamped}
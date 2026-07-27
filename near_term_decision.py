from near_term_check import get_battery_projection
from inverter import SNU, UTI, read_current_charger_mode_once, read_output_priority, set_charger_mode, set_output_priority, read_values_once
from db import log_mode_change


def apply_near_term_correction(conn, client):
    """
    Daytime-only, escalate-only correction: if the live projection shows
    the battery won't reach full by sunset, switch to SNU+UTI (if not
    already there). Never relaxes back toward OSO+SBU - that's Layer 1's
    job exclusively, once a day. Returns None if outside daylight hours
    or no risk detected (no action taken).
    """
    projection = get_battery_projection(conn)

    if projection is None:
        return None

    if projection["will_reach_full"]:
        return {"action": "no_change", "reason": "on track", "projection": projection}

    current_charger = read_current_charger_mode_once(client)
    current_output = read_output_priority(client)

    if current_charger == SNU and current_output == UTI:
        return {"action": "no_change", "reason": "already escalated", "projection": projection}

    set_charger_mode(client, SNU)
    set_output_priority(client, UTI)

    live_values = read_values_once(client)
    if live_values is not None:
        log_mode_change(conn, current_charger, SNU, "Layer 2: projected shortfall by sunset", live_values)

    return {"action": "escalated", "reason": "projected shortfall", "projection": projection}
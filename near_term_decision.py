from near_term_check import get_battery_projection
from inverter import read_current_charger_mode_once, read_output_priority, set_charger_mode, set_output_priority, read_values_once
from db import log_mode_change


def apply_near_term_correction(conn, client):
    """
    Daytime-only correction: recomputes today's tier using the shared
    classify_energy_balance() function with fresh live/short-range inputs,
    and applies it if different from the current state. NOTE: does not yet
    enforce "escalate only, never relax" - that's Issue #98, built next.
    """
    projection = get_battery_projection(conn)

    if projection is None:
        return None

    current_charger = read_current_charger_mode_once(client)
    current_output = read_output_priority(client)

    if current_charger == projection["charger_mode"] and current_output == projection["output_priority"]:
        return {"action": "no_change", "reason": "already at target", "projection": projection}

    set_charger_mode(client, projection["charger_mode"])
    set_output_priority(client, projection["output_priority"])

    live_values = read_values_once(client)
    if live_values is not None:
        log_mode_change(conn, current_charger, projection["charger_mode"], f"Layer 2: {projection['label']}", live_values)

    return {"action": "changed", "reason": projection["label"], "projection": projection}
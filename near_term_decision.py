from near_term_check import get_battery_projection
from inverter import OSO, SNU, UTI, SBU, read_current_charger_mode_once, read_output_priority, set_charger_mode, set_output_priority, read_values_once
from db import log_mode_change

# Escalation order, lowest to highest. An unrecognized (charger_mode,
# output_priority) combination - e.g. a manual override - is treated as
# rank 0, so the system can still escalate toward a known-correct state
# rather than getting stuck never intervening.
TIER_RANK = {
    (OSO, SBU): 0,
    (OSO, UTI): 1,
    (SNU, UTI): 2,
}


def get_tier_rank(charger_mode, output_priority):
    return TIER_RANK.get((charger_mode, output_priority), 0)


def apply_near_term_correction(conn, client):
    """
    Daytime-only correction: recomputes today's tier using the shared
    classify_energy_balance() function with fresh live/short-range inputs.
    Escalate-only: only applies the fresh tier if it's a STRICTLY HIGHER
    escalation than whatever is currently active - never relaxes anything
    mid-day. Only Layer 1's next daily run can relax the system back down.
    """
    projection = get_battery_projection(conn)

    if projection is None:
        return None

    current_charger = read_current_charger_mode_once(client)
    current_output = read_output_priority(client)

    current_rank = get_tier_rank(current_charger, current_output)
    fresh_rank = get_tier_rank(projection["charger_mode"], projection["output_priority"])

    if fresh_rank <= current_rank:
        return {
            "action": "no_change",
            "reason": "current state already at or above the fresh tier - Layer 2 does not relax",
            "projection": projection,
        }

    charger_success = set_charger_mode(client, projection["charger_mode"])
    output_success = set_output_priority(client, projection["output_priority"])

    if not charger_success or not output_success:
        return {"action": "write_failed", "reason": "one or both register writes failed", "projection": projection}

    live_values = read_values_once(client)
    if live_values is not None:
        log_mode_change(conn, current_charger, projection["charger_mode"], current_output, projection["output_priority"], f"Layer 2: {projection['label']}", live_values)

    return {"action": "escalated", "reason": projection["label"], "projection": projection}
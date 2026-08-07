from near_term_check import get_battery_projection
from actuator import apply_state
from inverter import OSO, SBU, OSO, UTI, SNU

TIER_RANK = {
    (OSO, SBU): 0,
    (OSO, UTI): 1,
    (SNU, UTI): 2,
}


def get_tier_rank(charger_mode, output_priority):
    return TIER_RANK.get((charger_mode, output_priority), 0)


def apply_near_term_correction(conn, client):
    """
    Daytime-only correction: gets Layer 2's fresh proposal (now via the
    shared pipeline), applies it only if it's a genuine escalation over
    current live state, using the tested actuator for the actual write.
    """
    proposal = get_battery_projection(conn)

    if proposal is None:
        return None

    from inverter import read_current_charger_mode_once, read_output_priority
    current_charger = read_current_charger_mode_once(client)
    current_output = read_output_priority(client)

    if current_charger is None or current_output is None:
        return {"action": "skipped", "reason": "could not read current state"}

    current_rank = get_tier_rank(current_charger, current_output)
    fresh_rank = get_tier_rank(proposal.charger_mode, proposal.output_priority)

    if fresh_rank <= current_rank:
        return {"action": "no_change", "reason": "current state already at or above the fresh tier - Layer 2 does not relax", "proposal": proposal}

    result = apply_state(client, conn, proposal.charger_mode, proposal.output_priority, f"Layer 2: {proposal.reason}")
    return {"action": "escalated" if result["action"] == "changed" else "write_failed", "proposal": proposal}
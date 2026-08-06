from near_term_decision import get_tier_rank


def arbitrate(current_charger, current_output, proposals):
    """
    Given the current live state and a list of proposals (some entries may
    be None - no active proposal this cycle from that source), picks the
    winning target state via the escalate-only policy, expressed here in
    exactly one place rather than smeared across three files.

    Returns the winning Proposal, or None if nothing should change (stay
    at current state).
    """
    current_rank = get_tier_rank(current_charger, current_output)

    # Layer 3 always wins if present, unconditionally - the live safety net
    layer3_proposal = next((p for p in proposals if p is not None and p.source == "layer3"), None)
    if layer3_proposal is not None:
        return layer3_proposal

    # Relax: only admitted if it's a GENUINE relaxation (strictly lower rank
    # than current) - that's its entire precondition. If it's proposing
    # something at or above current rank, it has nothing meaningful to add.
    relax_proposal = next((p for p in proposals if p is not None and p.source == "relax"), None)
    if relax_proposal is not None:
        relax_rank = get_tier_rank(relax_proposal.charger_mode, relax_proposal.output_priority)
        if relax_rank < current_rank:
            return relax_proposal

    # Everything else: escalate-only. Only admitted if strictly higher rank
    # than current; the highest-ranked candidate wins.
    escalation_candidates = [
        p for p in proposals
        if p is not None and p.source not in ("layer3", "relax")
    ]

    best_escalation = None
    best_rank = current_rank
    for p in escalation_candidates:
        p_rank = get_tier_rank(p.charger_mode, p.output_priority)
        if p_rank > best_rank:
            best_rank = p_rank
            best_escalation = p

    return best_escalation
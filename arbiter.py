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

    # Layer 1 is the once-daily AUTHORITATIVE reset - unlike everything
    # else, it applies regardless of direction (up OR down) when present
    # this cycle, since it represents a fresh, comprehensive re-analysis
    # of the whole day, not an incremental correction on top of whatever
    # is currently active. Real regression found live 2026-08-11: without
    # this, Layer 1's own "surplus, relax" conclusion could never actually
    # take effect once ANY escalation was already active - permanently
    # locking the system at whatever tier Rule 1/Layer 2 last reached,
    # even hours after Layer 1 itself determined that's no longer needed.
    layer1_proposal = next((p for p in proposals if p is not None and p.source == "layer1"), None)
    if layer1_proposal is not None:
        return layer1_proposal

    # Relax: only admitted if it's a GENUINE relaxation (strictly lower
    # rank than current) - that's its entire precondition.
    relax_proposal = next((p for p in proposals if p is not None and p.source == "relax"), None)
    if relax_proposal is not None:
        relax_rank = get_tier_rank(relax_proposal.charger_mode, relax_proposal.output_priority)
        if relax_rank < current_rank:
            return relax_proposal

    # Everything else: escalate-only. Only admitted if strictly higher
    # rank than current; the highest-ranked candidate wins.
    escalation_candidates = [
        p for p in proposals
        if p is not None and p.source not in ("layer3", "layer1", "relax")
    ]

    best_escalation = None
    best_rank = current_rank
    for p in escalation_candidates:
        p_rank = get_tier_rank(p.charger_mode, p.output_priority)
        if p_rank > best_rank:
            best_rank = p_rank
            best_escalation = p

    return best_escalation
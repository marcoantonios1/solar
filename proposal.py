from dataclasses import dataclass


@dataclass(frozen=True)
class Proposal:
    """
    A pure decision proposal from one layer - describes what state that
    layer would like the system to be in, and why. Carries no hardware
    interaction, no side effects - just data.

    Functions return a Proposal when they have an active recommendation,
    or None when they don't (e.g. Layer 3 not firing, Layer 2 outside
    daylight hours, relax not currently applicable). The arbiter is the
    only thing that decides which proposal (if any) actually gets applied.
    """
    charger_mode: int
    output_priority: int
    reason: str
    source: str  # "layer1", "layer2", "layer3", "relax"

    def __repr__(self):
        return f"Proposal(source={self.source!r}, charger={self.charger_mode}, output={self.output_priority}, reason={self.reason!r})"
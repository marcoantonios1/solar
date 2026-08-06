from config_loader import config
from inverter import SNU, UTI
from proposal import Proposal

CRITICAL_SOC_FLOOR = config["thresholds"]["low_soc_threshold"]


def evaluate_rules(values):
    """
    Layer 3 - the live safety net. Pure function: given current readings,
    returns a Proposal if the critical SOC floor is breached with EDL
    present, or None if the rule doesn't fire (no active opinion this
    cycle). Touches no hardware, needs no database access - the arbiter
    decides what to do with this proposal, not this function.
    """
    if values["battery_soc"] < CRITICAL_SOC_FLOOR and values["edl_present"]:
        return Proposal(
            charger_mode=SNU,
            output_priority=UTI,
            reason="Rule 1: critical SOC floor + EDL present",
            source="layer3"
        )

    return None
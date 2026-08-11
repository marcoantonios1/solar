from near_term_check import get_battery_projection
from inverter import OSO, SBU, UTI, SNU

TIER_RANK = {
    (OSO, SBU): 0,
    (OSO, UTI): 1,
    (SNU, UTI): 2,
}


def get_tier_rank(charger_mode, output_priority):
    return TIER_RANK.get((charger_mode, output_priority), 0)
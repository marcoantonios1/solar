from config_loader import config
from inverter import SNU, UTI

CRITICAL_SOC_FLOOR = config["thresholds"]["low_soc_threshold"]


def evaluate_rules(conn, values, current_mode):
    if values["battery_soc"] < CRITICAL_SOC_FLOOR and values["edl_present"]:
        return SNU, UTI, "Rule 1: critical SOC floor + EDL present"

    return None, None, "Default: no rule triggered"
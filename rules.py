from config_loader import config
from inverter import SNU, OSO

LOW_SOC_THRESHOLD = config["thresholds"]["low_soc_threshold"]
PV_MIN_THRESHOLD = config["thresholds"]["pv_min_threshold_w"]


def evaluate_rules(conn, values, current_mode):
    if (values["battery_soc"] < LOW_SOC_THRESHOLD
            and values["pv_power"] < PV_MIN_THRESHOLD
            and values["edl_present"]):
        return SNU, "Rule 1: low SOC + no sun + EDL present"

    return OSO, "Default: no rule triggered"
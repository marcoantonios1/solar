from config_loader import config
from db import is_load_sustained_high
from inverter import SNU, OSO

LOW_SOC_THRESHOLD = config["thresholds"]["low_soc_threshold"]
PV_MIN_THRESHOLD = config["thresholds"]["pv_min_threshold_w"]
LOAD_HIGH_THRESHOLD = config["thresholds"]["load_high_threshold_w"]
SUSTAINED_MINUTES = config["thresholds"]["sustained_high_load_minutes"]


def evaluate_rules(conn, values, current_mode):
    if (values["battery_soc"] < LOW_SOC_THRESHOLD
            and values["pv_power"] < PV_MIN_THRESHOLD
            and values["edl_present"]):
        return SNU, "Rule 1: low SOC + no sun + EDL present"

    if (values["edl_present"]
            and values["pv_power"] > PV_MIN_THRESHOLD
            and is_load_sustained_high(conn, SUSTAINED_MINUTES, LOAD_HIGH_THRESHOLD)):
        return SNU, f"Rule 2: load > {LOAD_HIGH_THRESHOLD}W sustained {SUSTAINED_MINUTES}min + solar present"

    return OSO, "Default: no rule triggered"
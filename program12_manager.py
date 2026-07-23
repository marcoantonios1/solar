from datetime import datetime

from config_loader import config
from inverter import read_program12, set_program12

DEFAULT_PERCENT = config["program12"]["default_percent"]
BOOST_PERCENT = 85  # raised threshold when a shortfall/recharge-risk is predicted
RECHARGE_RISK_MARGIN_KWH = 0.5  # treat a near-miss within this margin as a risk too


def should_boost_program12(predictions):
    """
    Returns True if today's prediction shows a real shortfall, a battery
    recharge risk (won't reach full, or is within RECHARGE_RISK_MARGIN_KWH
    of not reaching full), or if tomorrow's prediction shows a shortfall.
    """
    if not predictions:
        return False

    today = predictions[0]

    if today["classification"] == "shortfall":
        return True

    recharge = today.get("battery_recharge_status")
    if recharge is not None:
        if not recharge["will_reach_full"]:
            return True
        if recharge["net_after_recharge_kwh"] <= RECHARGE_RISK_MARGIN_KWH:
            return True

    if len(predictions) > 1 and predictions[1]["classification"] == "shortfall":
        return True

    return False


def apply_program12_decision(conn, client, predictions):
    """
    Decides whether to boost or reset Program 12 based on today's/tomorrow's
    predictions, applies the write, and returns (action, new_value) for logging.
    """
    current = read_program12(client)
    boost_needed = should_boost_program12(predictions)
    target = BOOST_PERCENT if boost_needed else DEFAULT_PERCENT

    if current is None:
        return "read_failed", None

    if current == target:
        return "no_change", current

    success = set_program12(client, target)
    if success:
        action = "boosted" if boost_needed else "reset_to_default"
        print(f"Program12 changed {current}% -> {target}% ({action})")
        return action, target
    else:
        return "write_failed", None
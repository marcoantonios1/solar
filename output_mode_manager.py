from inverter import (
    SBU, UTI, CSO, SNU, OSO,
    read_output_priority, set_output_priority,
    read_current_charger_mode_once, set_charger_mode,
    read_values_once
)
from config_loader import config
from db import log_mode_change

SHORTFALL_THRESHOLD_KWH = config["thresholds"]["shortfall_threshold_kwh"]
CHARGE_NEEDED_THRESHOLD_KWH = config["thresholds"]["charge_needed_threshold_kwh"]
TOMORROW_SHORTFALL_LOOKAHEAD_KWH = config["thresholds"]["tomorrow_shortfall_lookahead_kwh"]


def decide_target_state(predictions):
    """
    Returns (charger_mode, output_priority, label) based on today's prediction,
    AND a proactive check on tomorrow's forecast - if tomorrow looks like it
    will need EDL assistance, build extra battery buffer today even if today
    itself looks comfortable on its own.
    """
    today = predictions[0]
    balance = today["balance_kwh"]

    recharge = today.get("battery_recharge_status")
    recharge_shortfall = (
        recharge is not None and not recharge["will_reach_full"]
    )

    tomorrow_shortfall = False
    if len(predictions) > 1:
        tomorrow_balance = predictions[1]["balance_kwh"]
        tomorrow_shortfall = tomorrow_balance < TOMORROW_SHORTFALL_LOOKAHEAD_KWH

    if balance < CHARGE_NEEDED_THRESHOLD_KWH or recharge_shortfall:
        return SNU, UTI, "shortfall - charge + power house (SNU+UTI)"
    elif tomorrow_shortfall:
        return SNU, UTI, f"tomorrow predicted shortfall ({predictions[1]['balance_kwh']} kWh) - proactively building buffer today (SNU+UTI)"
    elif balance < SHORTFALL_THRESHOLD_KWH:
        return OSO, UTI, "small deficit - power house only, spare battery (OSO+UTI)"
    else:
        return OSO, SBU, "surplus - default, minimize EDL (OSO+SBU)"

def apply_output_mode_decision(client, conn, predictions):
    charger_target, output_target, label = decide_target_state(predictions)

    current_charger = read_current_charger_mode_once(client)
    current_output = read_output_priority(client)

    changed = False

    if current_charger != charger_target:
        set_charger_mode(client, charger_target)
        changed = True

    if current_output != output_target:
        set_output_priority(client, output_target)
        changed = True

    print(f"Decision: {label}")
    if changed:
        live_values = read_values_once(client)
        if live_values is not None:
            log_mode_change(conn, current_charger, charger_target, label, live_values)
        print(f"Applied: charger={charger_target}, output={output_target}")
    else:
        print("No change needed - already in target state.")

    return charger_target, output_target, label
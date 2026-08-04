from inverter import (
    SBU, UTI, CSO, SNU, OSO,
    read_output_priority, set_output_priority,
    read_current_charger_mode_once, set_charger_mode, read_values_once
)
from config_loader import config
from db import log_mode_change

SHORTFALL_THRESHOLD_KWH = config["thresholds"]["shortfall_threshold_kwh"]
CHARGE_NEEDED_THRESHOLD_KWH = config["thresholds"]["charge_needed_threshold_kwh"]
TOMORROW_SHORTFALL_LOOKAHEAD_KWH = config["thresholds"]["tomorrow_shortfall_lookahead_kwh"]


def classify_energy_balance(solar_expected_kwh, battery_available_kwh, house_expected_kwh):
    """
    Core, reusable three-tier decision - shared by Layer 1 (daily) and,
    once unified, Layer 2 (hourly). Takes the three raw inputs and returns
    (charger_mode, output_priority, label, balance_kwh) based on the basic
    surplus / small-deficit / large-deficit thresholds. Does NOT include
    Layer-1-specific extras (battery-recharge check, tomorrow's lookahead) -
    those stay as decide_target_state()'s own wrapper logic on top.
    """
    balance_kwh = solar_expected_kwh + battery_available_kwh - house_expected_kwh

    if balance_kwh < CHARGE_NEEDED_THRESHOLD_KWH:
        return SNU, UTI, "shortfall - charge + power house (SNU+UTI)", balance_kwh
    elif balance_kwh < SHORTFALL_THRESHOLD_KWH:
        return OSO, UTI, "small deficit - power house only, spare battery (OSO+UTI)", balance_kwh
    else:
        return OSO, SBU, "surplus - default, minimize EDL (OSO+SBU)", balance_kwh


def decide_target_state(predictions):
    """
    Returns (charger_mode, output_priority, label) based on today's basic
    tier (via classify_energy_balance), escalated further if either the
    battery-recharge check or tomorrow's forecast warrants it - these can
    only push the decision UP toward SNU+UTI, never relax a tier that
    classify_energy_balance already escalated to.
    """
    today = predictions[0]

    charger_mode, output_priority, label, balance_kwh = classify_energy_balance(
        solar_expected_kwh=today["solar_expected_kwh"],
        battery_available_kwh=today["battery_available_kwh"],
        house_expected_kwh=today["house_expected_kwh"],
    )

    recharge = today.get("battery_recharge_status")
    recharge_shortfall = (
        recharge is not None and not recharge["will_reach_full"]
    )

    tomorrow_shortfall = False
    if len(predictions) > 1:
        tomorrow_balance = predictions[1]["balance_kwh"]
        tomorrow_shortfall = tomorrow_balance < TOMORROW_SHORTFALL_LOOKAHEAD_KWH

    if charger_mode == SNU:
        return charger_mode, output_priority, label
    elif recharge_shortfall:
        return SNU, UTI, "shortfall - charge + power house (SNU+UTI)"
    elif tomorrow_shortfall:
        return SNU, UTI, f"tomorrow predicted shortfall ({predictions[1]['balance_kwh']} kWh) - proactively building buffer today (SNU+UTI)"
    else:
        return charger_mode, output_priority, label


def apply_output_mode_decision(client, conn, predictions):
    charger_target, output_target, label = decide_target_state(predictions)

    current_charger = read_current_charger_mode_once(client)
    current_output = read_output_priority(client)

    changed = False
    new_charger_value = current_charger
    new_output_value = current_output

    if current_charger != charger_target:
        charger_success = set_charger_mode(client, charger_target)
        if not charger_success:
            print("WARNING: charger mode write failed!")
        else:
            changed = True
            new_charger_value = charger_target

    if current_output != output_target:
        output_success = set_output_priority(client, output_target)
        if not output_success:
            print("WARNING: output priority write failed!")
        else:
            changed = True
            new_output_value = output_target

    print(f"Decision: {label}")
    if changed:
        live_values = read_values_once(client)
        if live_values is not None:
            log_mode_change(conn, current_charger, new_charger_value, current_output, new_output_value, label, live_values)
        print(f"Applied: charger={new_charger_value}, output={new_output_value}")
    else:
        print("No change needed - already in target state.")

    return charger_target, output_target, label
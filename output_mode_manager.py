from inverter import (
    SBU, UTI, CSO, SNU, OSO,
    read_output_priority, set_output_priority,
    read_current_charger_mode_once, set_charger_mode
)

SHORTFALL_THRESHOLD_KWH = 0.0        # any real shortfall triggers at least OSO+UTI
CHARGE_NEEDED_THRESHOLD_KWH = -1.0   # a more significant shortfall triggers SNU+UTI


def decide_target_state(predictions):
    """
    Returns (charger_mode, output_priority, label) based on today's prediction.
    - Comfortable surplus: OSO + SBU (default, minimize EDL)
    - Small deficit: OSO + UTI (EDL covers house load, spares battery, no charging spend)
    - Larger deficit: SNU + UTI (EDL charges battery fully AND powers house)
    """
    today = predictions[0]
    balance = today["balance_kwh"]

    recharge = today.get("battery_recharge_status")
    recharge_shortfall = (
        recharge is not None and not recharge["will_reach_full"]
    )

    if balance < CHARGE_NEEDED_THRESHOLD_KWH or recharge_shortfall:
        return SNU, UTI, "shortfall - charge + power house (SNU+UTI)"
    elif balance < SHORTFALL_THRESHOLD_KWH:
        return OSO, UTI, "small deficit - power house only, spare battery (OSO+UTI)"
    else:
        return OSO, SBU, "surplus - default, minimize EDL (OSO+SBU)"


def apply_output_mode_decision(client, predictions):
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
        print(f"Applied: charger={charger_target}, output={output_target}")
    else:
        print("No change needed - already in target state.")

    return charger_target, output_target, label
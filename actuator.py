import time
from inverter import (
    read_current_charger_mode_once, read_output_priority,
    set_charger_mode, set_output_priority, read_values_once
)
from db import log_mode_change
from utils import is_manual_mode

REGISTER_SETTLING_DELAY_SECONDS = 2


def apply_state(client, conn, target_charger, target_output, reason):
    """
    THE single place in the whole codebase that writes charger mode/output
    priority to the inverter. Waits for the register settling delay and
    reads back the actual applied value before trusting a write succeeded -
    the Modbus write command returning "success" only means the command was
    accepted, NOT that the register has actually settled to the new value yet.
    """
    if is_manual_mode():
        return {"action": "skipped", "reason": "MANUAL_MODE active"}

    current_charger = read_current_charger_mode_once(client)
    current_output = read_output_priority(client)

    if current_charger is None or current_output is None:
        return {"action": "skipped", "reason": "could not read current state - refusing to arbitrate blind"}

    new_charger_value = current_charger
    new_output_value = current_output
    charger_changed = False
    output_changed = False
    any_write_attempted = False

    if target_charger is not None and target_charger != current_charger:
        any_write_attempted = True
        command_accepted = set_charger_mode(client, target_charger)
        if not command_accepted:
            print(f"WARNING: charger mode write command rejected! (reason: {reason})")

    if target_output is not None and target_output != current_output:
        any_write_attempted = True
        command_accepted = set_output_priority(client, target_output)
        if not command_accepted:
            print(f"WARNING: output priority write command rejected! (reason: {reason})")

    if any_write_attempted:
        time.sleep(REGISTER_SETTLING_DELAY_SECONDS)

        actual_charger = read_current_charger_mode_once(client)
        actual_output = read_output_priority(client)

        if target_charger is not None and actual_charger == target_charger and actual_charger != current_charger:
            new_charger_value = actual_charger
            charger_changed = True
        elif target_charger is not None and target_charger != current_charger:
            print(f"WARNING: charger mode write did not take effect after settling delay! (reason: {reason})")

        if target_output is not None and actual_output == target_output and actual_output != current_output:
            new_output_value = actual_output
            output_changed = True
        elif target_output is not None and target_output != current_output:
            print(f"WARNING: output priority write did not take effect after settling delay! (reason: {reason})")

    if charger_changed or output_changed:
        live_values = read_values_once(client)
        if live_values is not None:
            log_mode_change(conn, current_charger, new_charger_value, current_output, new_output_value, reason, live_values)
        return {
            "action": "changed",
            "charger_changed": charger_changed,
            "output_changed": output_changed,
            "new_charger": new_charger_value,
            "new_output": new_output_value,
        }

    return {"action": "no_change"}
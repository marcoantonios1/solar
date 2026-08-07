import time
from inverter import (
    read_current_charger_mode_once, read_output_priority,
    set_charger_mode, set_output_priority, read_values_once
)
from db import log_mode_change
from utils import is_manual_mode
import time as time_module
from collections import deque
from config_loader import config

MODE_WRITE_MIN_INTERVAL_SECONDS = config["breaker_safety"].get("mode_write_min_interval_seconds", 60)
MAX_WRITES_PER_HOUR = config["breaker_safety"].get("max_writes_per_hour", 20)
REGISTER_SETTLING_DELAY_SECONDS = 2

_write_history = {"charger": deque(), "output": deque()}
_last_write_time = {"charger": None, "output": None}


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
        if not _write_guard_allows("charger"):
            print(f"WARNING: charger mode write blocked by WriteGuard! (reason: {reason})")
        else:
            command_accepted = set_charger_mode(client, target_charger)
            if not command_accepted:
                print(f"WARNING: charger mode write command rejected! (reason: {reason})")
            else:
                _record_write("charger")

    if target_output is not None and target_output != current_output:
        any_write_attempted = True
        if not _write_guard_allows("output"):
            print(f"WARNING: output priority write blocked by WriteGuard! (reason: {reason})")
        else:
            command_accepted = set_output_priority(client, target_output)
            if not command_accepted:
                print(f"WARNING: output priority write command rejected! (reason: {reason})")
            else:
                _record_write("output")

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

def _write_guard_allows(register_name):
    """
    EEPROM WriteGuard: holding-register writes persist to the inverter's
    EEPROM, which has finite write endurance. Enforces a minimum interval
    between writes to the same register, and a circuit breaker if writes
    are happening too frequently (a real bug oscillating writes could
    otherwise exhaust a cell in days - unfixable remotely).
    """
    now = time_module.time()

    last_write = _last_write_time[register_name]
    if last_write is not None and (now - last_write) < MODE_WRITE_MIN_INTERVAL_SECONDS:
        print(f"WriteGuard: blocked {register_name} write - too soon since last write ({now - last_write:.0f}s < {MODE_WRITE_MIN_INTERVAL_SECONDS}s minimum)")
        return False

    history = _write_history[register_name]
    while history and (now - history[0]) > 3600:
        history.popleft()

    if len(history) >= MAX_WRITES_PER_HOUR:
        print(f"WriteGuard: CIRCUIT BREAKER TRIPPED for {register_name} - {len(history)} writes in the last hour, refusing to write")
        return False

    return True


def _record_write(register_name):
    now = time_module.time()
    _last_write_time[register_name] = now
    _write_history[register_name].append(now)
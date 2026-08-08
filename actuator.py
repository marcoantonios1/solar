import time
from inverter import (
    read_current_charger_mode_once, read_output_priority,
    set_charger_mode, set_output_priority, read_values_once
)
from db import log_mode_change
from utils import is_manual_mode
from collections import deque
from config_loader import config
from alerts import send_alert
from db import log_mode_change, log_error

MODE_WRITE_MIN_INTERVAL_SECONDS = config["breaker_safety"].get("mode_write_min_interval_seconds", 60)
MAX_WRITES_PER_HOUR = config["breaker_safety"].get("max_writes_per_hour", 20)
REGISTER_SETTLING_DELAY_SECONDS = 2

_write_history = {"charger": deque(), "output": deque()}
_last_write_time = {"charger": None, "output": None}


def apply_state(client, conn, target_charger, target_output, reason):
    """
    THE single place in the whole codebase that writes charger mode/output
    priority to the inverter. Waits for the register settling delay and
    reads back the actual applied value before trusting a write succeeded.

    Real bug found 2026-08-07 (arbiter dry-run): if BOTH charger and output
    were requested but only ONE actually took effect, the old code still
    reported "changed" (success) - leaving the system silently sitting in
    an UNINTENDED combination (e.g. SNU+SBU, which physically blocks
    charging - confirmed live). Now: tracks exactly what was requested,
    retries a failed half immediately if the other half succeeded, and
    the result dict includes `fully_applied` so callers can tell "some
    single thing changed" apart from "everything I asked for happened".
    """
    if is_manual_mode():
        return {"action": "skipped", "reason": "MANUAL_MODE active"}

    current_charger = read_current_charger_mode_once(client)
    current_output = read_output_priority(client)

    if current_charger is None or current_output is None:
        return {"action": "skipped", "reason": "could not read current state - refusing to arbitrate blind"}

    charger_requested = target_charger is not None and target_charger != current_charger
    output_requested = target_output is not None and target_output != current_output

    new_charger_value = current_charger
    new_output_value = current_output
    charger_changed = False
    output_changed = False

    def _attempt_write(register_name, write_fn, target_value, attempt_reason, is_retry=False):
        if not _write_guard_allows(register_name, is_retry=is_retry):
            msg = f"WriteGuard blocked {register_name} write! (reason: {attempt_reason})"
            print(f"WARNING: {msg}")
            log_error(conn, "write_guard", msg)
            send_alert(conn, "actuator_write_failure", "EDL Solar: Write blocked/failed", msg)
            return False
        command_accepted = write_fn(client, target_value)
        if not command_accepted:
            msg = f"{register_name.capitalize()} write command rejected! (reason: {attempt_reason})"
            print(f"WARNING: {msg}")
            log_error(conn, "write_guard", msg)
            send_alert(conn, "actuator_write_failure", "EDL Solar: Write blocked/failed", msg)
            return False
        _record_write(register_name)
        return True

    any_write_attempted = False
    if charger_requested and _attempt_write("charger", set_charger_mode, target_charger, reason):
        any_write_attempted = True
    if output_requested and _attempt_write("output", set_output_priority, target_output, reason):
        any_write_attempted = True

    if any_write_attempted:
        time.sleep(REGISTER_SETTLING_DELAY_SECONDS)

        actual_charger = read_current_charger_mode_once(client)
        actual_output = read_output_priority(client)

        if charger_requested and actual_charger == target_charger:
            new_charger_value = actual_charger
            charger_changed = True
        elif charger_requested:
            print(f"WARNING: Charger mode write did not take effect after settling delay! (reason: {reason})")

        if output_requested and actual_output == target_output:
            new_output_value = actual_output
            output_changed = True
        elif output_requested:
            print(f"WARNING: Output priority write did not take effect after settling delay! (reason: {reason})")

        # PARTIAL FAILURE: both were requested, only one landed - we're
        # now sitting in a combination that's neither the old state nor
        # the intended new one. Retry the failed half immediately.
        charger_still_needs_retry = charger_requested and not charger_changed
        output_still_needs_retry = output_requested and not output_changed
        partial_failure = (charger_changed or output_changed) and (charger_still_needs_retry or output_still_needs_retry)

        if partial_failure:
            print(f"WARNING: PARTIAL WRITE FAILURE - retrying failed half immediately (reason: {reason})")

            if charger_still_needs_retry and _attempt_write("charger", set_charger_mode, target_charger, reason + " [retry]", is_retry=True):
                time.sleep(REGISTER_SETTLING_DELAY_SECONDS)
                actual_charger = read_current_charger_mode_once(client)
                if actual_charger == target_charger:
                    new_charger_value = actual_charger
                    charger_changed = True
                    charger_still_needs_retry = False

            if output_still_needs_retry and _attempt_write("output", set_output_priority, target_output, reason + " [retry]", is_retry=True):
                time.sleep(REGISTER_SETTLING_DELAY_SECONDS)
                actual_output = read_output_priority(client)
                if actual_output == target_output:
                    new_output_value = actual_output
                    output_changed = True
                    output_still_needs_retry = False

            if charger_still_needs_retry or output_still_needs_retry:
                critical_msg = (f"CRITICAL: partial write failure persisted after retry - system may be in an "
                                f"UNINTENDED combination (charger={new_charger_value}, output={new_output_value}). "
                                f"(reason: {reason})")
                print(f"WARNING: {critical_msg}")
                log_error(conn, "write_guard", critical_msg)
                send_alert(conn, "actuator_partial_failure", "EDL Solar: CRITICAL - partial write failure", critical_msg)

    if charger_changed or output_changed:
        live_values = read_values_once(client)
        if live_values is not None:
            log_mode_change(conn, current_charger, new_charger_value, current_output, new_output_value, reason, live_values)
        fully_applied = not (charger_requested and not charger_changed) and not (output_requested and not output_changed)
        return {
            "action": "changed",
            "charger_changed": charger_changed,
            "output_changed": output_changed,
            "new_charger": new_charger_value,
            "new_output": new_output_value,
            "fully_applied": fully_applied,
        }

    return {"action": "no_change"}

def _write_guard_allows(register_name, is_retry=False):
    """
    EEPROM WriteGuard. On a genuine RETRY of a write we already know
    failed, the minimum-interval check is deliberately bypassed - it
    exists to stop rapid, INDEPENDENT writes, not to block a single
    immediate retry of the same operation (real bug found 2026-08-08:
    every retry was being blocked by the guard recording the ORIGINAL
    attempt moments earlier, meaning retries could never actually
    succeed). The circuit breaker (max writes/hour) still applies
    either way - that's the real EEPROM protection.
    """
    now = time.time()

    if not is_retry:
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
    now = time.time()
    _last_write_time[register_name] = now
    _write_history[register_name].append(now)
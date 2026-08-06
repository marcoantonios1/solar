from datetime import datetime
from weather import fetch_current_weather
import time
import traceback
import sys

from config_loader import config
from inverter import (
    find_inverter_port, make_client, mode_name,
    read_values_with_retry, read_current_charger_mode_with_retry, set_charger_mode,
    read_output_priority, set_output_priority, SNU, UTI
)
from db import (
    init_db, save_reading, log_mode_change,
    get_open_edl_event, open_edl_event, close_edl_event, log_error, log_manual_mode_change
)
from rules import evaluate_rules
from utils import is_manual_mode, touch_heartbeat
from solar_model import get_expected_power, get_weather_adjusted_expected_power
from charge_throttle import adjust_charge_current_if_needed, relax_if_battery_full
from alerts import send_alert, clear_alert

POLL_INTERVAL_SECONDS = config["polling"]["interval_seconds"]
WEATHER_FETCH_INTERVAL_SECONDS = config["polling"]["weather_fetch_interval_seconds"]
FAST_POLL_INTERVAL_SECONDS = config["polling"]["fast_interval_seconds"]
ALERT_CRITICAL_SOC_THRESHOLD = config["thresholds"]["alert_critical_soc_threshold"]


def main():
    detected_port = find_inverter_port()
    client_holder = [make_client(detected_port)]

    if not client_holder[0].connect():
        print("Could not connect to inverter. Exiting.")
        sys.exit(1)

    conn = init_db()
    print("Connected. DB ready. Starting poll loop (Ctrl+C to stop)...")

    last_known_good_mode = None
    previous_edl_present = None
    last_weather_fetch_time = None
    cached_weather = None
    last_manual_mode_state = None

    open_event = get_open_edl_event(conn)
    if open_event:
        event_id, start_time = open_event
        print(f"Found open EDL event #{event_id} from before restart (started {start_time}).")

    while True:
        values = read_values_with_retry(client_holder, conn)

        if values is None:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] All read retries failed, skipping this cycle.")
            log_error(conn, "modbus_read", "All read retries failed")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        try:
            now = time.time()
            if last_weather_fetch_time is None or (now - last_weather_fetch_time) >= WEATHER_FETCH_INTERVAL_SECONDS:
                weather = fetch_current_weather()
                if weather is not None:
                    cached_weather = weather
                    last_weather_fetch_time = now

            values["cloud_cover"] = cached_weather["cloud_cover"] if cached_weather else None
            values["ambient_temp_c"] = cached_weather["ambient_temp_c"] if cached_weather else None

            if cached_weather and cached_weather.get("ambient_temp_c") is not None:
                expected = get_expected_power(ambient_temp_c=cached_weather["ambient_temp_c"])
                values["expected_pv_power"] = expected["expected_power_w"]
            else:
                values["expected_pv_power"] = None

            if cached_weather and all(cached_weather.get(k) is not None for k in ["ghi", "dni", "dhi", "ambient_temp_c"]):
                weather_expected = get_weather_adjusted_expected_power(
                    ghi=cached_weather["ghi"],
                    dni=cached_weather["dni"],
                    dhi=cached_weather["dhi"],
                    ambient_temp_c=cached_weather["ambient_temp_c"]
                )
                values["expected_pv_power_weather"] = weather_expected["expected_power_w"]
            else:
                values["expected_pv_power_weather"] = None

            print(values)
            save_reading(conn, values)

            current_edl_present = values["edl_present"]

            if previous_edl_present is None:
                open_event = get_open_edl_event(conn)
                if open_event and not current_edl_present:
                    event_id, start_time = open_event
                    close_edl_event(conn, event_id, values["timestamp"],
                                     note="Closed on restart - exact off time unknown, script was down")
                    print(f"Closed stale EDL event #{event_id} on restart (EDL was off when script resumed).")
                elif open_event and current_edl_present:
                    print(f"Resuming already-open EDL event #{open_event[0]} (EDL still on after restart).")
                elif not open_event and current_edl_present:
                    event_id = open_edl_event(conn, values["timestamp"])
                    print(f"EDL already on at startup, opened event #{event_id} (start time approximate).")
            else:
                if current_edl_present and not previous_edl_present:
                    event_id = open_edl_event(conn, values["timestamp"])
                    print(f"EDL turned ON -> opened event #{event_id}")
                elif not current_edl_present and previous_edl_present:
                    open_event = get_open_edl_event(conn)
                    if open_event:
                        event_id, _ = open_event
                        close_edl_event(conn, event_id, values["timestamp"])
                        print(f"EDL turned OFF -> closed event #{event_id}")

            previous_edl_present = current_edl_present

            current_mode = read_current_charger_mode_with_retry(client_holder[0])

            if current_mode is None:
                if last_known_good_mode is not None:
                    print(f"Mode read failed after retries. Falling back to last known good mode: {mode_name(last_known_good_mode)} (no write performed).")
                else:
                    print("Mode read failed after retries, and no prior known-good mode. Skipping decision logic this cycle.")
                touch_heartbeat()
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            last_known_good_mode = current_mode

            current_manual_mode = is_manual_mode()
            if current_manual_mode != last_manual_mode_state:
                log_manual_mode_change(conn, "on" if current_manual_mode else "off")
                last_manual_mode_state = current_manual_mode

            if current_manual_mode:
                print("MANUAL_MODE active - skipping mode-writing logic (readings still logged).")
                touch_heartbeat()
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            desired_mode, desired_output, reason = evaluate_rules(conn, values, current_mode)

            if values["battery_soc"] < ALERT_CRITICAL_SOC_THRESHOLD:
                send_alert(conn, "critical_soc",
                    f"EDL Solar: Critical SOC ({values['battery_soc']}%)",
                    f"Battery at {values['battery_soc']}% at {values['timestamp']}. EDL present: {values['edl_present']}.")
            else:
                clear_alert(conn, "critical_soc",
                    "EDL Solar: Critical SOC resolved",
                    f"Battery recovered to {values['battery_soc']}% at {values['timestamp']}.")

            current_output_before = read_output_priority(client_holder[0])
            new_charger_value = current_mode
            new_output_value = current_output_before
            charger_changed = False
            output_changed = False

            if desired_mode is not None and desired_mode != current_mode:
                success = set_charger_mode(client_holder[0], desired_mode)
                if success:
                    print(f"Mode changed -> {mode_name(desired_mode)} ({reason})")
                    last_known_good_mode = desired_mode
                    charger_changed = True
                    new_charger_value = desired_mode
                else:
                    print("Mode write failed!")

            if desired_output is not None and desired_output != current_output_before:
                output_success = set_output_priority(client_holder[0], desired_output)
                if output_success:
                    print(f"Output priority changed -> {desired_output} (Rule 1)")
                    output_changed = True
                    new_output_value = desired_output
                else:
                    print("WARNING: output priority write failed! (Rule 1)")

            if charger_changed or output_changed:
                log_mode_change(conn, current_mode, new_charger_value, current_output_before, new_output_value, reason, values)

            effective_mode = desired_mode if desired_mode is not None else current_mode

            current_output = read_output_priority(client_holder[0])
            throttle_result = adjust_charge_current_if_needed(
                client_holder[0], effective_mode, current_output, values["load_power"], values["pv_power"]
            )
            if throttle_result and throttle_result["action"] == "adjusted":
                print(f"Charge current adjusted: {throttle_result['from']}A -> {throttle_result['to']}A")

            touch_heartbeat()

            relax_result = relax_if_battery_full(client_holder[0], conn, effective_mode, current_output, values["battery_soc"])
            if relax_result:
                mode_desc = "OSO+UTI (preserving buffer for predicted cloudy tomorrow)" if relax_result["preserving_for_tomorrow"] else "OSO+SBU"
                print(f"Battery full ({relax_result['battery_soc']}%) -> relaxed to {mode_desc}")

            if effective_mode == SNU and current_output == UTI:
                time.sleep(FAST_POLL_INTERVAL_SECONDS)
            else:
                time.sleep(POLL_INTERVAL_SECONDS)

        except Exception as e:
            print(f"UNEXPECTED ERROR this cycle: {type(e).__name__}: {e}")
            log_error(conn, "crash", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            send_alert(conn, "crash",
                "EDL Solar: Unhandled crash",
                f"{type(e).__name__}: {e}\n\nSee system_errors table for full traceback.")
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
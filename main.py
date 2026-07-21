from datetime import datetime
from weather import fetch_current_weather
import time

from config_loader import config
from inverter import (
    find_inverter_port, make_client, mode_name,
    read_values_with_retry, read_current_charger_mode_with_retry, set_charger_mode
)
from db import (
    init_db, save_reading, log_mode_change,
    get_open_edl_event, open_edl_event, close_edl_event
)
from rules import evaluate_rules
from utils import is_manual_mode, touch_heartbeat
from solar_model import get_expected_power

POLL_INTERVAL_SECONDS = config["polling"]["interval_seconds"]
WEATHER_FETCH_INTERVAL_SECONDS = config["polling"]["weather_fetch_interval_seconds"]


def main():
    detected_port = find_inverter_port()
    client_holder = [make_client(detected_port)]

    if not client_holder[0].connect():
        print("Could not connect to inverter. Exiting.")
        return

    conn = init_db()
    print("Connected. DB ready. Starting poll loop (Ctrl+C to stop)...")

    last_known_good_mode = None
    previous_edl_present = None
    last_weather_fetch_time = None
    cached_weather = None

    open_event = get_open_edl_event(conn)
    if open_event:
        event_id, start_time = open_event
        print(f"Found open EDL event #{event_id} from before restart (started {start_time}).")

    while True:
        values = read_values_with_retry(client_holder)

        if values is None:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] All read retries failed, skipping this cycle.")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        now = time.time()
        if last_weather_fetch_time is None or (now - last_weather_fetch_time) >= WEATHER_FETCH_INTERVAL_SECONDS:
            weather = fetch_current_weather()
            if weather is not None:
                cached_weather = weather
                last_weather_fetch_time = now

        values["cloud_cover"] = cached_weather["cloud_cover"] if cached_weather else None
        
        if cached_weather and cached_weather.get("ambient_temp_c") is not None:
            expected = get_expected_power(ambient_temp_c=cached_weather["ambient_temp_c"])
            values["expected_pv_power"] = expected["expected_power_w"]
        else:
            values["expected_pv_power"] = None

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

        if is_manual_mode():
            print("MANUAL_MODE active - skipping mode-writing logic (readings still logged).")
            touch_heartbeat()
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        desired_mode, reason = evaluate_rules(conn, values, current_mode)

        if desired_mode != current_mode:
            success = set_charger_mode(client_holder[0], desired_mode)
            if success:
                print(f"Mode changed -> {mode_name(desired_mode)} ({reason})")
                log_mode_change(conn, current_mode, desired_mode, reason, values)
                last_known_good_mode = desired_mode
            else:
                print("Mode write failed!")

        touch_heartbeat()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
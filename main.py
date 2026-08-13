from datetime import datetime
from weather import fetch_current_weather
import time
import traceback
import sys

from config_loader import config
from actuator import apply_state
from inverter import (
    find_inverter_port, make_client, mode_name,
    read_values_with_retry, read_current_charger_mode_with_retry,
    read_output_priority, SNU, UTI
)
from db import (
    init_db, save_reading, log_mode_change,
    get_open_edl_event, open_edl_event, close_edl_event, log_error, log_manual_mode_change,
    log_daily_prediction, log_proposals
)
from rules import evaluate_rules
from arbiter import arbitrate
from utils import is_manual_mode, touch_heartbeat
from solar_model import get_expected_power, get_weather_adjusted_expected_power
from charge_throttle import adjust_charge_current_if_needed, relax_if_battery_full, relax_rule1_early_if_recovered
from alerts import send_alert
from near_term_check import get_battery_projection
from daily_predictor import get_daily_predictions
from output_mode_manager import decide_target_state

POLL_INTERVAL_SECONDS = config["polling"]["interval_seconds"]
WEATHER_FETCH_INTERVAL_SECONDS = config["polling"]["weather_fetch_interval_seconds"]
FAST_POLL_INTERVAL_SECONDS = config["polling"]["fast_interval_seconds"]
DAILY_LAYER1_HOUR = config["prediction"]["daily_layer1_hour"]


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
    last_layer2_run_hour = None
    pending_layer1_proposal = None

    today_str = datetime.now().strftime("%Y-%m-%d")
    existing_run = conn.execute(
        "SELECT 1 FROM daily_predictions WHERE date = ? LIMIT 1", (today_str,)
    ).fetchone()
    last_layer1_run_date = today_str if existing_run else None

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
                values["expected_pv_power_weather_raw"] = weather_expected["raw_expected_power_w"]
            else:
                values["expected_pv_power_weather"] = None
                values["expected_pv_power_weather_raw"] = None

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

            current_output_before = read_output_priority(client_holder[0])
            if current_output_before is None:
                print("Could not read output priority this cycle - skipping decision block (refusing to arbitrate blind)")
                touch_heartbeat()
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Issue #176: THE single decision point. Every layer below is a
            # PURE proposal generator - none apply anything themselves
            # anymore. All proposals get gathered and arbitrate() picks
            # exactly one winner, applied through the single actuator path.
            rule1_proposal = evaluate_rules(values)

            now_dt = datetime.now()
            layer1_proposal = pending_layer1_proposal
            if layer1_proposal is None and last_layer1_run_date != now_dt.strftime("%Y-%m-%d") and now_dt.hour >= DAILY_LAYER1_HOUR:
                try:
                    predictions = get_daily_predictions(conn)
                    if predictions:
                        layer1_proposal = decide_target_state(predictions, conn=conn)
                        run_timestamp = now_dt.isoformat(timespec="seconds")
                        log_daily_prediction(conn, run_timestamp, predictions[0], layer1_proposal.reason,
                                              layer1_proposal.charger_mode, layer1_proposal.output_priority)
                        print(f"[Layer 1] proposal: {layer1_proposal.reason}")
                    else:
                        print("[Layer 1] Could not fetch forecast - skipping this run.")
                        log_error(conn, "forecast_fetch", "daily prediction skipped in-loop - no forecast data")
                    last_layer1_run_date = now_dt.strftime("%Y-%m-%d")
                except Exception as e:
                    print(f"Layer 1 error: {e}")
                    log_error(conn, "crash", f"Layer 1 (in-loop): {type(e).__name__}: {e}\n{traceback.format_exc()}")

            layer2_proposal = None
            if last_layer2_run_hour != now_dt.hour:
                try:
                    layer2_proposal = get_battery_projection(conn)
                    if layer2_proposal is not None:
                        print(f"[Layer 2] proposal: {layer2_proposal.reason}")
                    last_layer2_run_hour = now_dt.hour
                except Exception as e:
                    print(f"Layer 2 error: {e}")
                    log_error(conn, "crash", f"Layer 2 (in-loop): {type(e).__name__}: {e}\n{traceback.format_exc()}")

            relax_proposal = relax_if_battery_full(conn, current_mode, current_output_before, values["battery_soc"])
            rule1_relax_proposal = relax_rule1_early_if_recovered(conn, current_mode, current_output_before, values["battery_soc"])

            all_proposals = [rule1_proposal, layer1_proposal, layer2_proposal, relax_proposal, rule1_relax_proposal]
            winner = arbitrate(current_mode, current_output_before, all_proposals)
            log_proposals(conn, now_dt.isoformat(timespec="seconds"), all_proposals, config.get("shadow_sources", []), winner)

            effective_mode = current_mode
            current_output = current_output_before

            if winner is not None:
                apply_result = apply_state(client_holder[0], conn, winner.charger_mode, winner.output_priority, winner.reason)
                if apply_result["action"] == "changed":
                    effective_mode = apply_result["new_charger"]
                    current_output = apply_result["new_output"]
                    if apply_result.get("fully_applied"):
                        print(f"Arbiter applied: {winner.reason}")
                    else:
                        print(f"WARNING: Arbiter decision only PARTIALLY applied: {winner.reason}")

                # Real gap found via external review 2026-08-11: Layer 1
                # only gets ONE cycle to apply (last_layer1_run_date is
                # already marked done). If apply_state() returns "skipped"
                # (transient read failure), the authoritative daily reset
                # would be lost until tomorrow - and since Layer 1 is the
                # only non-SOC-gated way to relax an escalation, a stale
                # escalation could hold all day. Keep retrying next cycle
                # until it applies for real (or a new day makes it moot).
                if winner is layer1_proposal and apply_result["action"] == "skipped":
                    pending_layer1_proposal = layer1_proposal
                    print("Layer 1 proposal could not be applied this cycle (transient read failure) - will retry next cycle")
                else:
                    pending_layer1_proposal = None
            else:
                pending_layer1_proposal = None

            throttle_result = adjust_charge_current_if_needed(
                client_holder[0], effective_mode, current_output, values["load_power"], values["pv_power"]
            )
            if throttle_result and throttle_result["action"] == "adjusted":
                print(f"Charge current adjusted: {throttle_result['from']}A -> {throttle_result['to']}A")

            touch_heartbeat()

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
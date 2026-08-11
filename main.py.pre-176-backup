from datetime import datetime
from weather import fetch_current_weather
import time
import traceback
import sys

from config_loader import config
from actuator import apply_state
from inverter import (
    find_inverter_port, make_client, mode_name,
    read_values_with_retry, read_current_charger_mode_with_retry, set_charger_mode,
    read_output_priority, set_output_priority, SNU, UTI
)
from db import (
    init_db, save_reading, log_mode_change,
    get_open_edl_event, open_edl_event, close_edl_event, log_error, log_manual_mode_change,
    log_daily_prediction
)
from rules import evaluate_rules
from arbiter import arbitrate
from utils import is_manual_mode, touch_heartbeat
from solar_model import get_expected_power, get_weather_adjusted_expected_power
from charge_throttle import adjust_charge_current_if_needed, relax_if_battery_full, relax_rule1_early_if_recovered
from alerts import send_alert, clear_alert
from near_term_decision import apply_near_term_correction
from daily_predictor import get_daily_predictions
from output_mode_manager import apply_output_mode_decision

POLL_INTERVAL_SECONDS = config["polling"]["interval_seconds"]
WEATHER_FETCH_INTERVAL_SECONDS = config["polling"]["weather_fetch_interval_seconds"]
FAST_POLL_INTERVAL_SECONDS = config["polling"]["fast_interval_seconds"]
ALERT_CRITICAL_SOC_THRESHOLD = config["thresholds"]["alert_critical_soc_threshold"]
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

    # Real bug fixed 2026-08-08: last_layer1_run_date was only tracked in
    # memory, so ANY restart after 7am reset it to None, causing Layer 1
    # to immediately re-fire with its stale morning calculation -
    # silently overwriting any more-accurate decision made since (e.g.
    # relax, manual correction, or a genuine nighttime escalation).
    # Now checks the database for whether Layer 1 genuinely already ran
    # today, so a restart can't cause this anymore.
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
            layer1_proposal_dry_run = None
            layer2_proposal_dry_run = None

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

            rule1_proposal = evaluate_rules(values)
            if rule1_proposal is not None:
                desired_mode = rule1_proposal.charger_mode
                desired_output = rule1_proposal.output_priority
                reason = rule1_proposal.reason
            else:
                desired_mode = None
                desired_output = None
                reason = "Default: no rule triggered"

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

            now_dt = datetime.now()

            if last_layer1_run_date != now_dt.strftime("%Y-%m-%d") and now_dt.hour >= DAILY_LAYER1_HOUR:
                try:
                    predictions = get_daily_predictions(conn)
                    if predictions:
                        from output_mode_manager import decide_target_state
                        layer1_proposal_dry_run = decide_target_state(predictions)
                        run_timestamp = now_dt.isoformat(timespec="seconds")
                        charger_mode, output_priority, label = apply_output_mode_decision(client_holder[0], conn, predictions)
                        log_daily_prediction(conn, run_timestamp, predictions[0], label, charger_mode, output_priority)
                        print(f"[Layer 1] {label}")
                    else:
                        print("[Layer 1] Could not fetch forecast - skipping this run.")
                        log_error(conn, "forecast_fetch", "daily prediction skipped in-loop - no forecast data")
                    last_layer1_run_date = now_dt.strftime("%Y-%m-%d")
                except Exception as e:
                    print(f"Layer 1 error: {e}")
                    log_error(conn, "crash", f"Layer 1 (in-loop): {type(e).__name__}: {e}\n{traceback.format_exc()}")

            if last_layer2_run_hour != now_dt.hour:
                try:
                    layer2_result = apply_near_term_correction(conn, client_holder[0])
                    if layer2_result is not None:
                        print(f"[Layer 2] {layer2_result['action']}: {layer2_result.get('proposal')}")
                        layer2_proposal_dry_run = layer2_result.get('proposal')
                    last_layer2_run_hour = now_dt.hour
                except Exception as e:
                    print(f"Layer 2 error: {e}")
                    log_error(conn, "crash", f"Layer 2 (in-loop): {type(e).__name__}: {e}\n{traceback.format_exc()}")

            relax_proposal = relax_if_battery_full(conn, effective_mode, current_output, values["battery_soc"])
            relax_apply_result = None
            if relax_proposal is not None:
                relax_apply_result = apply_state(client_holder[0], conn, relax_proposal.charger_mode, relax_proposal.output_priority, relax_proposal.reason)
                if relax_apply_result["action"] == "changed" and relax_apply_result.get("fully_applied"):
                    print(f"Battery full -> relaxed: {relax_proposal.reason}")
                elif relax_apply_result["action"] == "changed":
                    print(f"WARNING: Battery full relax only PARTIALLY applied: {relax_proposal.reason}")

            rule1_relax_proposal = relax_rule1_early_if_recovered(conn, effective_mode, current_output, values["battery_soc"])
            if rule1_relax_proposal is not None:
                rule1_relax_apply_result = apply_state(client_holder[0], conn, rule1_relax_proposal.charger_mode, rule1_relax_proposal.output_priority, rule1_relax_proposal.reason)
                if rule1_relax_apply_result["action"] == "changed" and rule1_relax_apply_result.get("fully_applied"):
                    print(f"Rule 1 early relax: {rule1_relax_proposal.reason}")
                elif rule1_relax_apply_result["action"] == "changed":
                    print(f"WARNING: Rule 1 early relax only PARTIALLY applied: {rule1_relax_proposal.reason}")

            # DRY RUN ONLY - Issue #176, Step 1: compute what the arbiter
            # WOULD decide given whatever proposals are available this
            # cycle, compare against what was actually applied. Logs any
            # disagreement but changes NOTHING - existing per-layer logic
            # remains the sole live decision path until this has been
            # validated against real cycles.
            try:
                dry_run_proposals = [rule1_proposal, layer1_proposal_dry_run, layer2_proposal_dry_run, relax_proposal]
                if any(p is not None for p in dry_run_proposals):
                    arbiter_winner = arbitrate(current_mode, current_output_before, dry_run_proposals)
                    final_charger = new_charger_value
                    final_output = new_output_value
                    if relax_apply_result and relax_apply_result.get("action") == "changed":
                        final_charger = relax_apply_result["new_charger"]
                        final_output = relax_apply_result["new_output"]
                    actual_state = (final_charger, final_output)
                    arbiter_state = (arbiter_winner.charger_mode, arbiter_winner.output_priority) if arbiter_winner else (current_mode, current_output_before)
                    if actual_state != arbiter_state:
                        msg = f"actual={actual_state}, arbiter_would_choose={arbiter_state}, arbiter_reasoning={arbiter_winner}"
                        print(f"[ARBITER DRY-RUN MISMATCH] {msg}")
                        log_error(conn, "arbiter_dry_run_mismatch", msg)
            except Exception as e:
                print(f"Arbiter dry-run error (non-fatal, comparison only): {e}")

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
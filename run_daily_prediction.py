from datetime import datetime

from inverter import find_inverter_port, make_client
from db import init_db, log_daily_prediction
from daily_predictor import get_daily_predictions
from program12_manager import apply_program12_decision


def main():
    detected_port = find_inverter_port()
    client = make_client(detected_port)

    if not client.connect():
        print("Could not connect to inverter. Exiting.")
        return

    conn = init_db()
    run_timestamp = datetime.now().isoformat(timespec="seconds")

    print(f"[{run_timestamp}] Running daily prediction...")

    predictions = get_daily_predictions(conn)

    if predictions is None:
        print("Could not fetch forecast - aborting this run.")
        client.close()
        return

    today_prediction = predictions[0]
    print(f"Today ({today_prediction['date']}): {today_prediction['classification']}, "
          f"balance {today_prediction['balance_kwh']} kWh")

    recharge = today_prediction.get("battery_recharge_status")
    if recharge:
        print(f"Battery recharge check: will_reach_full={recharge['will_reach_full']}, "
              f"net_after_recharge={recharge['net_after_recharge_kwh']} kWh")

    action, new_value = apply_program12_decision(conn, client, predictions)
    print(f"Program12 decision: {action} (value: {new_value})")

    log_daily_prediction(conn, run_timestamp, today_prediction, action, new_value)
    print("Logged to daily_predictions table.")

    client.close()


if __name__ == "__main__":
    main()
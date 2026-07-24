from datetime import datetime

from inverter import find_inverter_port, make_client
from db import init_db
from near_term_decision import apply_near_term_correction
from utils import is_manual_mode


def main():
    detected_port = find_inverter_port()
    client = make_client(detected_port)

    if not client.connect():
        print("Could not connect to inverter. Exiting.")
        return

    conn = init_db()
    run_timestamp = datetime.now().isoformat(timespec="seconds")
    print(f"[{run_timestamp}] Running near-term correction check...")

    if is_manual_mode():
        print("MANUAL_MODE active - skipping near-term correction.")
        client.close()
        return

    result = apply_near_term_correction(conn, client)

    if result is None:
        print("Outside daylight hours or no data - no action taken.")
    else:
        print(f"Action: {result['action']} ({result['reason']})")
        print(f"Projection: {result['projection']}")

    client.close()


if __name__ == "__main__":
    main()
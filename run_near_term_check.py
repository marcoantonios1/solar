from datetime import datetime
import traceback

from inverter import find_inverter_port, make_client
from db import init_db, log_error
from near_term_decision import apply_near_term_correction
from utils import is_manual_mode


def main():
    detected_port = find_inverter_port()
    client = make_client(detected_port)

    if not client.connect():
        print("Could not connect to inverter. Exiting.")
        return

    conn = init_db()

    try:
        run_timestamp = datetime.now().isoformat(timespec="seconds")
        print(f"[{run_timestamp}] Running near-term correction check...")

        if is_manual_mode():
            print("MANUAL_MODE active - skipping near-term correction.")
            return

        result = apply_near_term_correction(conn, client)

        if result is None:
            print("Outside daylight hours or no SOC data - no action taken.")
        else:
            print(f"Action: {result['action']}")
            print(f"Proposal: {result.get('proposal')}")

    except Exception as e:
        print(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")
        log_error(conn, "crash", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
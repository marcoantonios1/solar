import os
from datetime import datetime, timedelta

from db import init_db
from alerts import send_alert, clear_alert
from utils import HEARTBEAT_PATH

STALE_THRESHOLD_MINUTES = 10


def main():
    conn = init_db()

    if not os.path.exists(HEARTBEAT_PATH):
        send_alert(conn, "heartbeat_stale", "EDL Solar: Heartbeat file missing",
                    "The heartbeat file doesn't exist at all - service may never have started.")
        return

    with open(HEARTBEAT_PATH) as f:
        last_heartbeat = datetime.fromisoformat(f.read().strip())

    age = datetime.now() - last_heartbeat

    if age > timedelta(minutes=STALE_THRESHOLD_MINUTES):
        send_alert(conn, "heartbeat_stale", "EDL Solar: Heartbeat stale",
                    f"Last heartbeat was {age.total_seconds()/60:.1f} minutes ago ({last_heartbeat}). Service may be dead.")
    else:
        clear_alert(conn, "heartbeat_stale", "EDL Solar: Heartbeat resolved",
                     f"Heartbeat is fresh again ({age.total_seconds()/60:.1f} min old).")


if __name__ == "__main__":
    main()
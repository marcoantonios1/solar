import os
from datetime import datetime

HEARTBEAT_PATH = "last_updated.txt"
MANUAL_MODE_FLAG_PATH = "MANUAL_MODE"


def is_manual_mode():
    return os.path.exists(MANUAL_MODE_FLAG_PATH)


def touch_heartbeat():
    with open(HEARTBEAT_PATH, "w") as f:
        f.write(datetime.now().isoformat(timespec="seconds"))
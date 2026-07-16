from pymodbus.client import ModbusSerialClient
from datetime import datetime
import sqlite3
import json
import time
import glob
import os

CONFIG_PATH = 'config.json'

with open(CONFIG_PATH) as f:
    config = json.load(f)

DB_PATH = config["database"]["path"]
POLL_INTERVAL_SECONDS = config["polling"]["interval_seconds"]
DEVICE_ID = config["modbus"]["device_id"]

LOW_SOC_THRESHOLD = config["thresholds"]["low_soc_threshold"]
PV_MIN_THRESHOLD = config["thresholds"]["pv_min_threshold_w"]
LOAD_HIGH_THRESHOLD = config["thresholds"]["load_high_threshold_w"]
SUSTAINED_MINUTES = config["thresholds"]["sustained_high_load_minutes"]

CHARGER_PRIORITY_REG = config["registers"]["charger_priority"]["address"]

CSO = 0
SNU = 1
OSO = 2

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2
HEARTBEAT_PATH = "last_updated.txt"
MANUAL_MODE_FLAG_PATH = "MANUAL_MODE"


def mode_name(mode_value):
    return {CSO: "CSO", SNU: "SNU", OSO: "OSO"}.get(mode_value, str(mode_value))

def is_manual_mode():
    return os.path.exists(MANUAL_MODE_FLAG_PATH)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            pv_power REAL,
            battery_soc INTEGER,
            load_power REAL,
            edl_present INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mode_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            old_mode TEXT,
            new_mode TEXT,
            trigger_reason TEXT,
            battery_soc INTEGER,
            pv_power REAL,
            load_power REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edl_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            duration_min REAL,
            avg_pv_power_during REAL,
            total_kwh_charged_during REAL,
            reason TEXT,
            cost_usd REAL
        )
    """)
    conn.commit()
    return conn


def find_inverter_port():
    """
    Scans likely serial device paths and tries each one, returning the first
    that successfully responds to a Modbus read. Falls back to config.json's
    configured port if no candidate works.
    """
    candidates = sorted(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*'))

    for candidate in candidates:
        test_client = ModbusSerialClient(
            port=candidate,
            baudrate=config["modbus"]["baudrate"],
            parity=config["modbus"]["parity"],
            stopbits=config["modbus"]["stopbits"],
            bytesize=config["modbus"]["bytesize"],
            timeout=2
        )
        try:
            if test_client.connect():
                result = test_client.read_input_registers(18, count=1, device_id=DEVICE_ID)
                test_client.close()
                if not result.isError():
                    print(f"Found inverter on {candidate}")
                    return candidate
        except Exception:
            pass
        finally:
            test_client.close()

    print(f"No responsive device found among {candidates}, falling back to config.json port.")
    return config["modbus"]["port"]

def read_values_once(client):
    """Single attempt, no retry. Returns dict or None."""
    try:
        pv_result = client.read_input_registers(3, count=2, device_id=DEVICE_ID)
        bat_result = client.read_input_registers(18, count=1, device_id=DEVICE_ID)
        load_result = client.read_input_registers(9, count=2, device_id=DEVICE_ID)
        grid_result = client.read_input_registers(20, count=1, device_id=DEVICE_ID)

        if any(r.isError() for r in [pv_result, bat_result, load_result, grid_result]):
            return None

        pv_power = pv_result.registers[1] / 10
        battery_soc = bat_result.registers[0]
        load_power = load_result.registers[1] / 10
        grid_voltage = grid_result.registers[0] / 10
        edl_present = grid_voltage > 100

        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "pv_power": pv_power,
            "battery_soc": battery_soc,
            "load_power": load_power,
            "edl_present": edl_present,
        }
    except Exception as e:
        print(f"Read error: {e}")
        return None


def read_values_with_retry(client_holder):
    for attempt in range(1, MAX_RETRIES + 1):
        values = read_values_once(client_holder[0])
        if values is not None:
            return values
        print(f"Read attempt {attempt}/{MAX_RETRIES} failed.")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)

    print("All retries failed. Attempting to re-detect the inverter's port...")

    try:
        client_holder[0].close()
    except Exception:
        pass

    new_port = None
    for scan_attempt in range(1, 4):
        time.sleep(3)
        new_port = find_inverter_port()
        if new_port:
            break
        print(f"Port scan attempt {scan_attempt}/3 found nothing, retrying...")

    if not new_port:
        print("Could not find inverter after rescanning. Will retry next cycle.")
        return None

    print(f"Reconnecting on {new_port}...")
    client_holder[0] = ModbusSerialClient(
        port=new_port,
        baudrate=config["modbus"]["baudrate"],
        parity=config["modbus"]["parity"],
        stopbits=config["modbus"]["stopbits"],
        bytesize=config["modbus"]["bytesize"],
        timeout=3
    )
    client_holder[0].connect()
    return read_values_once(client_holder[0])


def read_current_charger_mode_once(client):
    try:
        result = client.read_holding_registers(CHARGER_PRIORITY_REG, count=1, device_id=DEVICE_ID)
        if result.isError():
            return None
        return result.registers[0]
    except Exception as e:
        print(f"Mode read error: {e}")
        return None


def read_current_charger_mode_with_retry(client):
    for attempt in range(1, MAX_RETRIES + 1):
        mode = read_current_charger_mode_once(client)
        if mode is not None:
            return mode
        print(f"Mode read attempt {attempt}/{MAX_RETRIES} failed.")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)
    return None


def set_charger_mode(client, mode_value):
    try:
        result = client.write_register(CHARGER_PRIORITY_REG, mode_value, device_id=DEVICE_ID)
        return not result.isError()
    except Exception as e:
        print(f"Mode write error: {e}")
        return False


def save_reading(conn, values):
    conn.execute(
        "INSERT INTO readings (timestamp, pv_power, battery_soc, load_power, edl_present) VALUES (?, ?, ?, ?, ?)",
        (
            values["timestamp"],
            values["pv_power"],
            values["battery_soc"],
            values["load_power"],
            int(values["edl_present"]),
        )
    )
    conn.commit()


def log_mode_change(conn, old_mode, new_mode, reason, values):
    conn.execute(
        """INSERT INTO mode_changes
           (timestamp, old_mode, new_mode, trigger_reason, battery_soc, pv_power, load_power)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            values["timestamp"],
            mode_name(old_mode),
            mode_name(new_mode),
            reason,
            values["battery_soc"],
            values["pv_power"],
            values["load_power"],
        )
    )
    conn.commit()


def is_load_sustained_high(conn, minutes, threshold):
    cursor = conn.execute(
        """SELECT load_power FROM readings
           WHERE timestamp > datetime('now', ?)
           ORDER BY timestamp DESC""",
        (f'-{minutes} minutes',)
    )
    rows = cursor.fetchall()
    if not rows:
        return False
    return all(row[0] > threshold for row in rows)


def evaluate_rules(conn, values, current_mode):
    if (values["battery_soc"] < LOW_SOC_THRESHOLD
            and values["pv_power"] < PV_MIN_THRESHOLD
            and values["edl_present"]):
        return SNU, "Rule 1: low SOC + no sun + EDL present"

    if (values["edl_present"]
            and values["pv_power"] > PV_MIN_THRESHOLD
            and is_load_sustained_high(conn, SUSTAINED_MINUTES, LOAD_HIGH_THRESHOLD)):
        return SNU, f"Rule 2: load > {LOAD_HIGH_THRESHOLD}W sustained {SUSTAINED_MINUTES}min + solar present"

    return OSO, "Default: no rule triggered"


def touch_heartbeat():
    with open(HEARTBEAT_PATH, "w") as f:
        f.write(datetime.now().isoformat(timespec="seconds"))

def get_open_edl_event(conn):
    """Returns the currently open edl_events row (end_time IS NULL), if any."""
    cursor = conn.execute(
        "SELECT event_id, start_time FROM edl_events WHERE end_time IS NULL ORDER BY event_id DESC LIMIT 1"
    )
    return cursor.fetchone()  # (event_id, start_time) or None


def open_edl_event(conn, start_time):
    cursor = conn.execute(
        "INSERT INTO edl_events (start_time) VALUES (?)",
        (start_time,)
    )
    conn.commit()
    return cursor.lastrowid


def close_edl_event(conn, event_id, end_time, note=None):
    row = conn.execute(
        "SELECT start_time FROM edl_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row is None:
        return
    start_time = datetime.fromisoformat(row[0])
    end_dt = datetime.fromisoformat(end_time)
    duration_min = (end_dt - start_time).total_seconds() / 60

    cursor = conn.execute(
        """SELECT AVG(pv_power) FROM readings
           WHERE timestamp >= ? AND timestamp <= ?""",
        (row[0], end_time)
    )
    avg_pv = cursor.fetchone()[0]

    reason = note if note else "EDL session closed normally"

    conn.execute(
        """UPDATE edl_events
           SET end_time = ?, duration_min = ?, avg_pv_power_during = ?, reason = ?
           WHERE event_id = ?""",
        (end_time, duration_min, avg_pv, reason, event_id)
    )
    conn.commit()


def main():
    detected_port = find_inverter_port()
    client_holder = [ModbusSerialClient(
        port=detected_port,
        baudrate=config["modbus"]["baudrate"],
        parity=config["modbus"]["parity"],
        stopbits=config["modbus"]["stopbits"],
        bytesize=config["modbus"]["bytesize"],
        timeout=3
    )]

    if not client_holder[0].connect():
        print("Could not connect to inverter. Exiting.")
        return

    conn = init_db()
    print("Connected. DB ready. Starting poll loop (Ctrl+C to stop)...")

    last_known_good_mode = None

    # Handle a leftover open event from before a restart
    open_event = get_open_edl_event(conn)
    if open_event:
        event_id, start_time = open_event
        print(f"Found open EDL event #{event_id} from before restart (started {start_time}).")

    previous_edl_present = None  # unknown at startup, set on first real reading

    while True:
        values = read_values_with_retry(client_holder)

        if values is None:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] All read retries failed, skipping this cycle.")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        print(values)
        save_reading(conn, values)

        save_reading(conn, values)

        current_edl_present = values["edl_present"]

        if previous_edl_present is None:
            # First reading since startup - reconcile with any open event
            open_event = get_open_edl_event(conn)
            if open_event and not current_edl_present:
                # We restarted, EDL was on before, but is now off - close it with a note
                event_id, start_time = open_event
                close_edl_event(conn, event_id, values["timestamp"],
                                 note="Closed on restart - exact off time unknown, script was down")
                print(f"Closed stale EDL event #{event_id} on restart (EDL was off when script resumed).")
            elif open_event and current_edl_present:
                print(f"Resuming already-open EDL event #{open_event[0]} (EDL still on after restart).")
            elif not open_event and current_edl_present:
                # EDL is on but no open event exists - open one now, we missed the true start
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
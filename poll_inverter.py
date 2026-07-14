from pymodbus.client import ModbusSerialClient
from datetime import datetime
import sqlite3
import json
import time

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
    conn.commit()
    return conn


def read_values(client):
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


def read_current_charger_mode(client):
    try:
        result = client.read_holding_registers(CHARGER_PRIORITY_REG, count=1, device_id=DEVICE_ID)
        if result.isError():
            return None
        return result.registers[0]
    except Exception as e:
        print(f"Mode read error: {e}")
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
            "SNU" if old_mode == SNU else "CSO" if old_mode == CSO else str(old_mode),
            "SNU" if new_mode == SNU else "CSO",
            reason,
            values["battery_soc"],
            values["pv_power"],
            values["load_power"],
        )
    )
    conn.commit()


def is_load_sustained_high(conn, minutes, threshold):
    """
    Returns True if every reading in the last `minutes` minutes had
    load_power above `threshold`. Requires at least one reading in that
    window to avoid a false positive on startup.
    """
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
    """
    Returns the mode the system SHOULD be in, and a reason string.
    Rule 1 (low SOC, no sun) takes priority over Rule 2.
    Defaults to CSO if no rule fires.
    """
    # Rule 1: low battery, no sun -> allow EDL to help charge
    if (values["battery_soc"] < LOW_SOC_THRESHOLD
            and values["pv_power"] < PV_MIN_THRESHOLD
            and values["edl_present"]):
        return SNU, "Rule 1: low SOC + no sun + EDL present"

    # Rule 2: sustained high load with solar present -> let EDL assist
    if (values["edl_present"]
            and values["pv_power"] > PV_MIN_THRESHOLD
            and is_load_sustained_high(conn, SUSTAINED_MINUTES, LOAD_HIGH_THRESHOLD)):
        return SNU, f"Rule 2: load > {LOAD_HIGH_THRESHOLD}W sustained {SUSTAINED_MINUTES}min + solar present"

    # Default
    return CSO, "Default: no rule triggered"


def main():
    client = ModbusSerialClient(
        port=config["modbus"]["port"],
        baudrate=config["modbus"]["baudrate"],
        parity=config["modbus"]["parity"],
        stopbits=config["modbus"]["stopbits"],
        bytesize=config["modbus"]["bytesize"],
        timeout=3
    )

    if not client.connect():
        print("Could not connect to inverter. Exiting.")
        return

    conn = init_db()
    print("Connected. DB ready. Starting poll loop (Ctrl+C to stop)...")

    while True:
        values = read_values(client)

        if values is None:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] Read failed, skipping this cycle.")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        print(values)
        save_reading(conn, values)

        current_mode = read_current_charger_mode(client)
        if current_mode is None:
            print("Could not read current charger mode, skipping decision logic this cycle.")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        desired_mode, reason = evaluate_rules(conn, values, current_mode)

        if desired_mode != current_mode:
            success = set_charger_mode(client, desired_mode)
            if success:
                mode_name = "SNU" if desired_mode == SNU else "CSO"
                print(f"Mode changed -> {mode_name} ({reason})")
                log_mode_change(conn, current_mode, desired_mode, reason, values)
            else:
                print("Mode write failed!")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
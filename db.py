import sqlite3
from datetime import datetime

from config_loader import config
from inverter import mode_name

DB_PATH = config["database"]["path"]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            pv_power REAL,
            battery_soc INTEGER,
            load_power REAL,
            edl_present INTEGER,
            ac_charge_power REAL
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


def save_reading(conn, values):
    conn.execute(
        "INSERT INTO readings (timestamp, pv_power, battery_soc, load_power, edl_present, ac_charge_power) VALUES (?, ?, ?, ?, ?, ?)",
        (
            values["timestamp"],
            values["pv_power"],
            values["battery_soc"],
            values["load_power"],
            int(values["edl_present"]),
            values["ac_charge_power"],
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


def get_open_edl_event(conn):
    cursor = conn.execute(
        "SELECT event_id, start_time FROM edl_events WHERE end_time IS NULL ORDER BY event_id DESC LIMIT 1"
    )
    return cursor.fetchone()


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
    duration_hours = duration_min / 60

    cursor = conn.execute(
        """SELECT AVG(pv_power), AVG(ac_charge_power) FROM readings
           WHERE timestamp >= ? AND timestamp <= ?""",
        (row[0], end_time)
    )
    avg_pv, avg_ac_charge_power = cursor.fetchone()

    total_kwh_charged_during = None
    if avg_ac_charge_power is not None and duration_hours > 0:
        total_kwh_charged_during = (avg_ac_charge_power * duration_hours) / 1000

    reason = note if note else "EDL session closed normally"

    conn.execute(
        """UPDATE edl_events
           SET end_time = ?, duration_min = ?, avg_pv_power_during = ?,
               total_kwh_charged_during = ?, reason = ?
           WHERE event_id = ?""",
        (end_time, duration_min, avg_pv, total_kwh_charged_during, reason, event_id)
    )
    conn.commit()
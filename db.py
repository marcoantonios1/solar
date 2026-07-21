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
            ac_charge_power REAL,
            cloud_cover REAL,
            expected_pv_power REAL
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
        "INSERT INTO readings (timestamp, pv_power, battery_soc, load_power, edl_present, ac_charge_power, cloud_cover, expected_pv_power) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            values["timestamp"],
            values["pv_power"],
            values["battery_soc"],
            values["load_power"],
            int(values["edl_present"]),
            values["ac_charge_power"],
            values.get("cloud_cover"),
            values.get("expected_pv_power"),
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

def find_trigger_reason(conn, start_time, window_minutes=10):
    """
    Looks for the most recent mode_changes entry at or shortly before start_time,
    within window_minutes. Returns its trigger_reason, or None if nothing matches.
    """
    cursor = conn.execute(
        """SELECT trigger_reason FROM mode_changes
           WHERE timestamp <= ? AND timestamp >= datetime(?, ?)
           ORDER BY timestamp DESC LIMIT 1""",
        (start_time, start_time, f'-{window_minutes} minutes')
    )
    row = cursor.fetchone()
    return row[0] if row else None


def calculate_event_cost(conn, event_id, kwh_this_event, start_time):
    """
    Calculates the $ cost of this event's kWh, accounting for the tiered rate.
    Looks at all OTHER events already recorded this calendar month to determine
    how much tier-1 allowance is left before this event's kWh applies.
    """
    if kwh_this_event is None:
        return None

    tier1_rate = config["edl_tariff"]["tier1_rate_usd_per_kwh"]
    tier1_limit = config["edl_tariff"]["tier1_limit_kwh"]
    tier2_rate = config["edl_tariff"]["tier2_rate_usd_per_kwh"]

    month_start = start_time[:7] + "-01T00:00:00"

    cursor = conn.execute(
        """SELECT COALESCE(SUM(total_kwh_charged_during), 0) FROM edl_events
           WHERE start_time >= ? AND start_time < ? AND event_id != ?""",
        (month_start, start_time, event_id)
    )
    prior_kwh_this_month = cursor.fetchone()[0]

    remaining_tier1 = max(tier1_limit - prior_kwh_this_month, 0)
    kwh_at_tier1 = min(kwh_this_event, remaining_tier1)
    kwh_at_tier2 = kwh_this_event - kwh_at_tier1

    cost = (kwh_at_tier1 * tier1_rate) + (kwh_at_tier2 * tier2_rate)
    return round(cost, 4)


def close_edl_event(conn, event_id, end_time, note=None):
    row = conn.execute(
        "SELECT start_time FROM edl_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row is None:
        return
    start_time_str = row[0]
    start_time = datetime.fromisoformat(start_time_str)
    end_dt = datetime.fromisoformat(end_time)
    duration_min = (end_dt - start_time).total_seconds() / 60
    duration_hours = duration_min / 60

    cursor = conn.execute(
        """SELECT AVG(pv_power), AVG(ac_charge_power) FROM readings
           WHERE timestamp >= ? AND timestamp <= ?""",
        (start_time_str, end_time)
    )
    avg_pv, avg_ac_charge_power = cursor.fetchone()

    total_kwh_charged_during = None
    if avg_ac_charge_power is not None and duration_hours > 0:
        total_kwh_charged_during = (avg_ac_charge_power * duration_hours) / 1000

    trigger_reason = find_trigger_reason(conn, start_time_str)

    if trigger_reason:
        reason = trigger_reason
    elif note:
        reason = note
    else:
        reason = "No matching mode change found - manual override or EDL already allowed"

    cost_usd = calculate_event_cost(conn, event_id, total_kwh_charged_during, start_time_str)

    conn.execute(
        """UPDATE edl_events
           SET end_time = ?, duration_min = ?, avg_pv_power_during = ?,
               total_kwh_charged_during = ?, reason = ?, cost_usd = ?
           WHERE event_id = ?""",
        (end_time, duration_min, avg_pv, total_kwh_charged_during, reason, cost_usd, event_id)
    )
    conn.commit()
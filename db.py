import sqlite3
from datetime import datetime

from config_loader import config
from inverter import mode_name

DB_PATH = config["database"]["path"]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings(timestamp)")
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
            expected_pv_power REAL,
            expected_pv_power_weather REAL,
            ambient_temp_c REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mode_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            old_mode TEXT,
            new_mode TEXT,
            old_output TEXT,
            new_output TEXT,
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp TEXT NOT NULL,
            date TEXT NOT NULL,
            solar_expected_kwh REAL,
            house_expected_kwh REAL,
            battery_available_kwh REAL,
            balance_kwh REAL,
            classification TEXT,
            shortfall_kwh REAL,
            decision_label TEXT,
            charger_mode TEXT,
            output_priority TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            category TEXT NOT NULL,
            message TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS manual_mode_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            state TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS battery_cumulative_stats (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            seeded_prior_cycles REAL DEFAULT 0,
            cumulative_cycles REAL DEFAULT 0,
            last_calculated_through TEXT
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO battery_cumulative_stats (id, seeded_prior_cycles, cumulative_cycles, last_calculated_through)
        VALUES (1, 0, 0, NULL)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_log (
            alert_key TEXT PRIMARY KEY,
            last_sent TEXT NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts TEXT NOT NULL,
            source TEXT,
            charger_mode INTEGER,
            output_priority INTEGER,
            reason TEXT,
            shadow INTEGER NOT NULL,
            was_executed INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


def save_reading(conn, values):
    conn.execute(
        "INSERT INTO readings (timestamp, pv_power, battery_soc, load_power, edl_present, ac_charge_power, cloud_cover, expected_pv_power, expected_pv_power_weather, ambient_temp_c) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            values["timestamp"],
            values["pv_power"],
            values["battery_soc"],
            values["load_power"],
            int(values["edl_present"]),
            values["ac_charge_power"],
            values.get("cloud_cover"),
            values.get("expected_pv_power"),
            values.get("expected_pv_power_weather"),
            values.get("ambient_temp_c"),
        )
    )
    conn.commit()


def log_mode_change(conn, old_mode, new_mode, old_output, new_output, reason, values):
    conn.execute(
        """INSERT INTO mode_changes
           (timestamp, old_mode, new_mode, old_output, new_output, trigger_reason, battery_soc, pv_power, load_power)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            values["timestamp"],
            mode_name(old_mode),
            mode_name(new_mode),
            old_output,
            new_output,
            reason,
            values["battery_soc"],
            values["pv_power"],
            values["load_power"],
        )
    )
    conn.commit()

def log_error(conn, category, message):
    conn.execute(
        "INSERT INTO system_errors (timestamp, category, message) VALUES (?, ?, ?)",
        (datetime.now().isoformat(timespec="seconds"), category, message)
    )
    conn.commit()


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

def log_daily_prediction(conn, run_timestamp, prediction, decision_label, charger_mode, output_priority):
    conn.execute(
        """INSERT INTO daily_predictions
           (run_timestamp, date, solar_expected_kwh, house_expected_kwh, battery_available_kwh,
            balance_kwh, classification, shortfall_kwh, decision_label, charger_mode, output_priority)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_timestamp, prediction["date"], prediction["solar_expected_kwh"],
            prediction["house_expected_kwh"], prediction["battery_available_kwh"],
            prediction["balance_kwh"], prediction["classification"], prediction["shortfall_kwh"],
            decision_label, mode_name(charger_mode), str(output_priority)
        )
    )
    conn.commit()

def log_manual_mode_change(conn, state):
    conn.execute(
        "INSERT INTO manual_mode_log (timestamp, state) VALUES (?, ?)",
        (datetime.now().isoformat(timespec="seconds"), state)
    )
    conn.commit()

def log_proposals(conn, run_ts, proposals, shadow_sources, winner):
    """
    Logs EVERY proposal generated this cycle (shadow and non-shadow alike)
    for later comparison - the automated, permanent version of the manual
    dry-run comparison built for #176. was_executed reflects whether THIS
    specific proposal was the one the arbiter actually applied.
    """
    for p in proposals:
        if p is None:
            continue
        is_shadow = p.source in shadow_sources
        was_executed = (winner is not None and p is winner)
        conn.execute(
            """INSERT INTO proposals (run_ts, source, charger_mode, output_priority, reason, shadow, was_executed)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_ts, p.source, p.charger_mode, p.output_priority, p.reason, int(is_shadow), int(was_executed))
        )
    conn.commit()
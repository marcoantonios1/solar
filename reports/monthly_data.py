import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from datetime import datetime, timedelta

from config_loader import config
from reports.report import tiered_cost, estimate_old_way_kwh, categorize_reason

DB_PATH = config["database"]["path"]
CRITICAL_SOC_FLOOR = config["thresholds"]["low_soc_threshold"]
CAPACITY_KWH_USABLE = config["battery"]["capacity_kwh_usable"]
UNDERPERFORMANCE_THRESHOLD_PCT = config["performance_monitoring"]["underperformance_threshold_pct"]
CLEAR_SKY_CLOUD_THRESHOLD_PCT = 20


def integrate_power_kwh(conn, column, start, end):
    """
    Trapezoidal integration of a power column (pv_power or load_power) over
    a time range, in kWh. Same method report.py already uses for the
    'old way' house-demand estimate.
    """
    rows = conn.execute(
        f"""SELECT timestamp, {column} FROM readings
            WHERE timestamp >= ? AND timestamp <= ? AND {column} IS NOT NULL
            ORDER BY timestamp ASC""",
        (start, end)
    ).fetchall()

    if len(rows) < 2:
        return 0.0

    total_kwh = 0.0
    for i in range(len(rows) - 1):
        t1, p1 = rows[i]
        t2, p2 = rows[i + 1]
        dt1 = datetime.fromisoformat(t1)
        dt2 = datetime.fromisoformat(t2)
        hours = (dt2 - dt1).total_seconds() / 3600
        avg_power = (p1 + p2) / 2
        total_kwh += (avg_power * hours) / 1000

    return round(total_kwh, 2)


def get_monthly_totals(conn, start, end):
    total_solar_kwh = integrate_power_kwh(conn, "pv_power", start, end)
    total_house_kwh = integrate_power_kwh(conn, "load_power", start, end)

    events = conn.execute(
        """SELECT total_kwh_charged_during, cost_usd FROM edl_events
           WHERE start_time >= ? AND start_time <= ? AND end_time IS NOT NULL""",
        (start, end)
    ).fetchall()

    charged_sessions = [e for e in events if e[0] and e[0] > 0]
    blocked_sessions = [e for e in events if not e[0] or e[0] == 0]

    total_edl_kwh = sum(e[0] or 0 for e in events)
    total_edl_cost = sum(e[1] or 0 for e in events)

    return {
        "total_solar_kwh": total_solar_kwh,
        "total_house_kwh": total_house_kwh,
        "total_edl_sessions": len(events),
        "edl_sessions_charged": len(charged_sessions),
        "edl_sessions_blocked": len(blocked_sessions),
        "total_edl_kwh": round(total_edl_kwh, 2),
        "total_edl_cost": round(total_edl_cost, 4),
    }


def get_executive_summary(conn, start, end, days):
    totals = get_monthly_totals(conn, start, end)

    old_way_kwh = estimate_old_way_kwh(conn, start, end)
    old_way_cost = tiered_cost(old_way_kwh)
    savings = old_way_cost - totals["total_edl_cost"]

    longest_event = conn.execute(
        """SELECT event_id, start_time, end_time, duration_min, cost_usd FROM edl_events
           WHERE start_time >= ? AND start_time <= ? AND end_time IS NOT NULL
           ORDER BY duration_min DESC LIMIT 1""",
        (start, end)
    ).fetchone()

    return {
        "total_solar_kwh": totals["total_solar_kwh"],
        "total_edl_cost": totals["total_edl_cost"],
        "old_way_cost_estimate": round(old_way_cost, 2),
        "estimated_savings": round(savings, 2),
        "longest_edl_event": longest_event,
        "period_days": days,
    }


def get_daily_breakdown(conn, start, end):
    start_date = datetime.fromisoformat(start).date()
    end_date = datetime.fromisoformat(end).date()

    days = []
    current = start_date
    while current <= end_date:
        day_start = f"{current.isoformat()}T00:00:00"
        day_end = f"{current.isoformat()}T23:59:59"

        solar_kwh = integrate_power_kwh(conn, "pv_power", day_start, day_end)
        house_kwh = integrate_power_kwh(conn, "load_power", day_start, day_end)

        events = conn.execute(
            """SELECT start_time, end_time, total_kwh_charged_during, cost_usd FROM edl_events
               WHERE start_time >= ? AND start_time <= ? AND end_time IS NOT NULL""",
            (day_start, day_end)
        ).fetchall()

        charged = [e for e in events if e[2] and e[2] > 0]
        blocked = [e for e in events if not e[2] or e[2] == 0]

        days.append({
            "date": current.isoformat(),
            "solar_kwh": solar_kwh,
            "house_kwh": house_kwh,
            "edl_sessions_charged": len(charged),
            "edl_sessions_blocked": len(blocked),
            "edl_kwh": round(sum(e[2] or 0 for e in events), 2),
            "edl_cost": round(sum(e[3] or 0 for e in events), 4),
            "edl_session_times": [(e[0], e[1]) for e in events],
        })

        current += timedelta(days=1)

    return days


def get_solar_performance(conn, start, end):
    rows = conn.execute(
        """SELECT timestamp, pv_power, expected_pv_power_weather, cloud_cover FROM readings
           WHERE timestamp >= ? AND timestamp <= ?
           AND expected_pv_power_weather IS NOT NULL
           AND expected_pv_power_weather >= 200""",
        (start, end)
    ).fetchall()

    if not rows:
        return {
            "avg_gap_pct": None, "avg_cloud_cover": None,
            "clear_days": 0, "cloudy_days": 0, "underperformance_flag_count": 0,
        }

    gaps = [((r[1] - r[2]) / r[2]) * 100 for r in rows]
    avg_gap = sum(gaps) / len(gaps)

    cloud_by_day = {}
    for timestamp, _, _, cloud in rows:
        date_str = timestamp[:10]
        if cloud is not None:
            cloud_by_day.setdefault(date_str, []).append(cloud)

    clear_days = sum(1 for vals in cloud_by_day.values() if sum(vals) / len(vals) <= CLEAR_SKY_CLOUD_THRESHOLD_PCT)
    cloudy_days = len(cloud_by_day) - clear_days

    avg_cloud = None
    all_cloud_vals = [c for vals in cloud_by_day.values() for c in vals]
    if all_cloud_vals:
        avg_cloud = sum(all_cloud_vals) / len(all_cloud_vals)

    underperf_count = sum(1 for g in gaps if g <= UNDERPERFORMANCE_THRESHOLD_PCT)

    return {
        "avg_gap_pct": round(avg_gap, 1),
        "avg_cloud_cover": round(avg_cloud, 1) if avg_cloud is not None else None,
        "clear_days": clear_days,
        "cloudy_days": cloudy_days,
        "underperformance_reading_count": underperf_count,
    }


def get_battery_health(conn, start, end):
    row = conn.execute(
        "SELECT MIN(battery_soc) FROM readings WHERE timestamp >= ? AND timestamp <= ? AND battery_soc IS NOT NULL",
        (start, end)
    ).fetchone()
    lowest_soc = row[0]

    near_critical_count = conn.execute(
        "SELECT COUNT(*) FROM readings WHERE timestamp >= ? AND timestamp <= ? AND battery_soc < ?",
        (start, end, CRITICAL_SOC_FLOOR + 5)
    ).fetchone()[0]

    # Rough cycle estimate: sum of net discharge (load - pv - ac_charge, when positive)
    # integrated over time, divided by usable capacity
    rows = conn.execute(
        """SELECT timestamp, pv_power, load_power, ac_charge_power FROM readings
           WHERE timestamp >= ? AND timestamp <= ?
           AND pv_power IS NOT NULL AND load_power IS NOT NULL
           ORDER BY timestamp ASC""",
        (start, end)
    ).fetchall()

    discharge_kwh = 0.0
    for i in range(len(rows) - 1):
        t1, pv1, load1, ac1 = rows[i]
        t2, pv2, load2, ac2 = rows[i + 1]
        net1 = load1 - pv1 - (ac1 or 0)
        net2 = load2 - pv2 - (ac2 or 0)
        avg_net = (net1 + net2) / 2
        if avg_net > 0:
            dt1 = datetime.fromisoformat(t1)
            dt2 = datetime.fromisoformat(t2)
            hours = (dt2 - dt1).total_seconds() / 3600
            discharge_kwh += (avg_net * hours) / 1000

    rough_cycles = round(discharge_kwh / CAPACITY_KWH_USABLE, 2) if CAPACITY_KWH_USABLE else None

    return {
        "lowest_soc_pct": lowest_soc,
        "readings_near_critical_floor": near_critical_count,
        "rough_cycle_estimate": rough_cycles,
        "note": "Cycle estimate is approximate - derived from load/solar/EDL-charge readings, not direct battery current measurement.",
    }


def get_system_health(conn, start, end):
    modbus_errors = conn.execute(
        "SELECT COUNT(*) FROM system_errors WHERE timestamp >= ? AND timestamp <= ? AND category = 'modbus_read'",
        (start, end)
    ).fetchone()[0]

    crash_count = conn.execute(
        "SELECT COUNT(*) FROM system_errors WHERE timestamp >= ? AND timestamp <= ? AND category = 'crash'",
        (start, end)
    ).fetchone()[0]

    manual_mode_rows = conn.execute(
        "SELECT timestamp, state FROM manual_mode_log WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
        (start, end)
    ).fetchall()

    manual_mode_seconds = 0.0
    manual_mode_tracked = len(manual_mode_rows) > 0
    on_since = None
    for timestamp, state in manual_mode_rows:
        if state == "on":
            on_since = datetime.fromisoformat(timestamp)
        elif state == "off" and on_since is not None:
            manual_mode_seconds += (datetime.fromisoformat(timestamp) - on_since).total_seconds()
            on_since = None

    readings_timestamps = conn.execute(
        "SELECT timestamp FROM readings WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
        (start, end)
    ).fetchall()

    longest_gap_minutes = 0.0
    for i in range(len(readings_timestamps) - 1):
        t1 = datetime.fromisoformat(readings_timestamps[i][0])
        t2 = datetime.fromisoformat(readings_timestamps[i + 1][0])
        gap = (t2 - t1).total_seconds() / 60
        longest_gap_minutes = max(longest_gap_minutes, gap)

    return {
        "modbus_read_failures": modbus_errors,
        "unhandled_crashes": crash_count,
        "manual_mode_hours": round(manual_mode_seconds / 3600, 2) if manual_mode_tracked else None,
        "manual_mode_tracked": manual_mode_tracked,
        "longest_logging_gap_minutes": round(longest_gap_minutes, 1),
    }


def get_full_monthly_report_data(days=30):
    conn = sqlite3.connect(DB_PATH)
    end = datetime.now()
    start = end - timedelta(days=days)
    start_str = start.isoformat(timespec="seconds")
    end_str = end.isoformat(timespec="seconds")

    data = {
        "period_start": start_str,
        "period_end": end_str,
        "period_days": days,
        "executive_summary": get_executive_summary(conn, start_str, end_str, days),
        "monthly_totals": get_monthly_totals(conn, start_str, end_str),
        "daily_breakdown": get_daily_breakdown(conn, start_str, end_str),
        "solar_performance": get_solar_performance(conn, start_str, end_str),
        "battery_health": get_battery_health(conn, start_str, end_str),
        "system_health": get_system_health(conn, start_str, end_str),
    }

    conn.close()
    return data
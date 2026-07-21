import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3
from datetime import datetime, timedelta

from config_loader import config

DB_PATH = config["database"]["path"]
TIER1_RATE = config["edl_tariff"]["tier1_rate_usd_per_kwh"]
TIER1_LIMIT = config["edl_tariff"]["tier1_limit_kwh"]
TIER2_RATE = config["edl_tariff"]["tier2_rate_usd_per_kwh"]


def tiered_cost(kwh):
    """Simple tiered cost for a standalone kWh figure (used for the 'old way' estimate)."""
    tier1_kwh = min(kwh, TIER1_LIMIT)
    tier2_kwh = max(kwh - TIER1_LIMIT, 0)
    return (tier1_kwh * TIER1_RATE) + (tier2_kwh * TIER2_RATE)


def categorize_reason(reason):
    if not reason:
        return "Unknown"
    if reason.startswith("Rule 1"):
        return "Rule 1 (low SOC + no sun)"
    if reason.startswith("Rule 2"):
        return "Rule 2 (sustained high load)"
    if "cutoff" in reason.lower() or "Program 12" in reason:
        return "Battery hit Program 12 cutoff"
    if "restart" in reason.lower() or "manual" in reason.lower():
        return "Manual / restart (not automation-driven)"
    return "Other / no matching rule"


def estimate_old_way_kwh(conn, start, end):
    """
    Integrates load_power over the period using trapezoidal approximation
    between consecutive readings, to estimate total house energy demand.
    This represents what EDL would have had to supply under the OLD
    always-on behavior (no SBU/solar prioritization).
    """
    rows = conn.execute(
        """SELECT timestamp, load_power FROM readings
           WHERE timestamp >= ? AND timestamp <= ?
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

    return total_kwh


def generate_report(days):
    conn = sqlite3.connect(DB_PATH)
    end = datetime.now()
    start = end - timedelta(days=days)
    start_str = start.isoformat(timespec="seconds")
    end_str = end.isoformat(timespec="seconds")

    print(f"\n{'='*60}")
    print(f"EDL / Solar Automation Report")
    print(f"Period: {start_str} to {end_str} ({days} days)")
    print(f"{'='*60}\n")

    events = conn.execute(
        """SELECT event_id, start_time, end_time, duration_min,
                  total_kwh_charged_during, cost_usd, reason
           FROM edl_events
           WHERE start_time >= ? AND start_time <= ? AND end_time IS NOT NULL
           ORDER BY start_time ASC""",
        (start_str, end_str)
    ).fetchall()

    total_sessions = len(events)
    total_duration_min = sum(e[3] or 0 for e in events)
    total_kwh = sum(e[4] or 0 for e in events)
    total_cost = sum(e[5] or 0 for e in events)

    print(f"Total EDL sessions: {total_sessions}")
    print(f"Total EDL duration: {total_duration_min:.1f} minutes ({total_duration_min/60:.2f} hours)")
    print(f"Total kWh delivered by EDL: {total_kwh:.4f} kWh")
    print(f"Total actual cost: ${total_cost:.4f}")

    print(f"\n--- Breakdown by trigger reason ---")
    breakdown = {}
    for e in events:
        category = categorize_reason(e[6])
        if category not in breakdown:
            breakdown[category] = {"sessions": 0, "kwh": 0.0, "cost": 0.0}
        breakdown[category]["sessions"] += 1
        breakdown[category]["kwh"] += e[4] or 0
        breakdown[category]["cost"] += e[5] or 0

    if breakdown:
        for category, stats in breakdown.items():
            print(f"  {category}: {stats['sessions']} session(s), "
                  f"{stats['kwh']:.4f} kWh, ${stats['cost']:.4f}")
    else:
        print("  No EDL sessions in this period.")

    print(f"\n--- Comparison: automated vs. old always-on EDL ---")
    old_way_kwh = estimate_old_way_kwh(conn, start_str, end_str)
    old_way_cost = tiered_cost(old_way_kwh)

    print(f"Estimated total house demand this period: {old_way_kwh:.2f} kWh")
    print(f"Estimated cost if EDL had supplied ALL of it (old behavior): ${old_way_cost:.2f}")
    print(f"Actual EDL cost with automation: ${total_cost:.4f}")

    if old_way_cost > 0:
        savings = old_way_cost - total_cost
        pct_saved = (savings / old_way_cost) * 100
        print(f"Estimated savings: ${savings:.2f} ({pct_saved:.1f}%)")

    print(f"\nNote: the 'old way' comparison assumes EDL would have supplied 100% of")
    print(f"house load during this period, with no solar/battery contribution at all.")
    print(f"This is a simplified worst-case baseline, not a measurement of actual")
    print(f"past EDL-only usage.")
    print(f"{'='*60}\n")

    conn.close()


if __name__ == "__main__":
    days = 7
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            print(f"Invalid days argument '{sys.argv[1]}', defaulting to 7.")
    generate_report(days)
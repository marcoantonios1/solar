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
GENERATOR_RATE = config["generator"]["rate_usd_per_kwh"]


def tiered_cost(kwh):
    """Simple tiered cost for a standalone kWh figure (used for the 'old way' estimate)."""
    tier1_kwh = min(kwh, TIER1_LIMIT)
    tier2_kwh = max(kwh - TIER1_LIMIT, 0)
    return (tier1_kwh * TIER1_RATE) + (tier2_kwh * TIER2_RATE)


def categorize_reason(reason):
    if not reason:
        return "Unknown"

    # Strip known source prefixes to get at the underlying tier label,
    # re-attached afterward so the category still shows which layer/
    # mechanism produced it
    core_reason = reason
    source_prefix = ""
    if reason.startswith("Layer 2:"):
        source_prefix = "Layer 2 - "
        core_reason = reason[len("Layer 2:"):].strip()
    elif reason.startswith("Relax (battery full):"):
        source_prefix = "Relax - "
        core_reason = reason[len("Relax (battery full):"):].strip()

    if core_reason.startswith("Rule 1"):
        return "Rule 1 (critical SOC floor)"
    if "tomorrow predicted shortfall" in core_reason:
        return source_prefix + "Tomorrow lookahead (proactive buffer)"
    if "shortfall" in core_reason.lower() or "SNU+UTI" in core_reason:
        return source_prefix + "Shortfall (SNU+UTI)"
    if "small deficit" in core_reason.lower() or "OSO+UTI" in core_reason:
        return source_prefix + "Small deficit (OSO+UTI)"
    if "surplus" in core_reason.lower() or "OSO+SBU" in core_reason:
        return source_prefix + "Surplus / default (OSO+SBU)"
    if "restart" in core_reason.lower():
        return "Restart (stale event closed)"
    if "manual" in core_reason.lower():
        return "Manual (not automation-driven)"

    return "Other / no matching rule"


def estimate_old_way_kwh_split(conn, start, end):
    """
    Same trapezoidal integration as before, but split into two totals based
    on real logged edl_present status: kWh during hours EDL was actually
    available (old way would have used EDL/grid rate) vs. kWh during hours
    it wasn't (old way would have used the private generator instead, at
    the much higher flat rate) - a more realistic baseline than assuming
    EDL was available 100% of the time.
    """
    rows = conn.execute(
        """SELECT timestamp, load_power, edl_present FROM readings
           WHERE timestamp >= ? AND timestamp <= ?
           ORDER BY timestamp ASC""",
        (start, end)
    ).fetchall()

    if len(rows) < 2:
        return 0.0, 0.0

    edl_available_kwh = 0.0
    edl_unavailable_kwh = 0.0

    for i in range(len(rows) - 1):
        t1, p1, edl1 = rows[i]
        t2, p2, _ = rows[i + 1]
        dt1 = datetime.fromisoformat(t1)
        dt2 = datetime.fromisoformat(t2)
        hours = (dt2 - dt1).total_seconds() / 3600
        avg_power = (p1 + p2) / 2
        kwh = (avg_power * hours) / 1000

        if edl1:
            edl_available_kwh += kwh
        else:
            edl_unavailable_kwh += kwh

    return edl_available_kwh, edl_unavailable_kwh


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

    print(f"\n--- Comparison: automated vs. old always-on behavior ---")
    edl_available_kwh, edl_unavailable_kwh = estimate_old_way_kwh_split(conn, start_str, end_str)
    old_way_cost = tiered_cost(edl_available_kwh) + (edl_unavailable_kwh * GENERATOR_RATE)

    print(f"Estimated house demand during EDL-available hours: {edl_available_kwh:.2f} kWh (would use EDL @ tiered rate)")
    print(f"Estimated house demand during EDL-unavailable hours: {edl_unavailable_kwh:.2f} kWh (would use generator @ ${GENERATOR_RATE}/kWh)")
    print(f"Estimated old-way cost (realistic, EDL + generator mix): ${old_way_cost:.2f}")
    print(f"Actual EDL cost with automation: ${total_cost:.4f}")

    if old_way_cost > 0:
        savings = old_way_cost - total_cost
        pct_saved = (savings / old_way_cost) * 100
        print(f"Estimated savings: ${savings:.2f} ({pct_saved:.1f}%)")

    print(f"\nNote: the 'old way' comparison now realistically accounts for the fact that")
    print(f"when EDL isn't available, the private generator (${GENERATOR_RATE}/kWh) would have")
    print(f"been used instead - split using real logged edl_present data, not assumed")
    print(f"100% EDL availability. Still a simplified baseline, not a measurement of")
    print(f"actual past behavior before automation.")
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
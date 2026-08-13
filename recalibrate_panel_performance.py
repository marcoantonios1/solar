"""
Recalibrates the temporary panel-performance derate factor (config.json ->
panels.temporary_performance_derate) from real, accumulated data - a
reusable, re-runnable version of the one-off analysis done 2026-08-13.

Reports the current vs recommended value. Never auto-applies silently -
this is a safety-relevant parameter (feeds into every layer's decision),
so a human reviews the number before it changes anything live. Run this
periodically (e.g. monthly, or after cleaning the panels) to keep the
derate honest as real performance changes over time.

Usage:
    python3 recalibrate_panel_performance.py           # report only
    python3 recalibrate_panel_performance.py --apply    # report AND update config.json
"""
import sqlite3
import json
import argparse

from config_loader import config

DB_PATH = config["database"]["path"]
MIN_EXPECTED_POWER_FOR_COMPARISON = config["performance_monitoring"]["min_expected_power_for_comparison_w"]

# Known points where the derate factor itself changed - readings after each
# point need the THEN-current factor divided out to normalize back to the
# raw, un-derated model before combining with other periods. Add a new
# entry here each time the factor is manually changed, so future
# recalibrations stay correctly normalized across the full history.
DERATE_CHANGE_HISTORY = [
    {"effective_from": "2026-08-12T00:00:00", "factor": 0.75},
    {"effective_from": "2026-08-13T12:00:00", "factor": 0.8},
]


def normalize_expected(timestamp, expected):
    """Divides out whatever derate factor was active at this reading's time, reconstructing the raw model's original prediction."""
    active_factor = 1.0
    for change in DERATE_CHANGE_HISTORY:
        if timestamp >= change["effective_from"]:
            active_factor = change["factor"]
    return expected / active_factor


def recalibrate():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT timestamp, pv_power, expected_pv_power_weather FROM readings
           WHERE expected_pv_power_weather IS NOT NULL AND expected_pv_power_weather >= ?
           ORDER BY timestamp ASC""",
        (MIN_EXPECTED_POWER_FOR_COMPARISON,)
    ).fetchall()

    if not rows:
        print("No readings with sufficient expected power found - nothing to calibrate against.")
        return None

    gaps = []
    for timestamp, actual, expected in rows:
        normalized_expected = normalize_expected(timestamp, expected)
        gap_pct = ((actual - normalized_expected) / normalized_expected) * 100
        gaps.append(gap_pct)

    avg_gap_pct = sum(gaps) / len(gaps)
    recommended_factor = round((100 + avg_gap_pct) / 100, 3)

    current_factor = config["panels"].get("temporary_performance_derate", 1.0)

    print(f"Readings analyzed: {len(gaps)}")
    print(f"Average gap vs raw (un-derated) model: {avg_gap_pct:.1f}%")
    print(f"Current derate factor: {current_factor}")
    print(f"Recommended derate factor: {recommended_factor}")

    return recommended_factor


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write the recommended value to config.json")
    args = parser.parse_args()

    recommended = recalibrate()

    if recommended is not None and args.apply:
        with open("config.json") as f:
            cfg = json.load(f)
        cfg["panels"]["temporary_performance_derate"] = recommended
        with open("config.json", "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"\nconfig.json updated. Remember to add a new DERATE_CHANGE_HISTORY entry above with today's date and this new factor, then restart edl-solar.service.")
    elif recommended is not None:
        print("\n(Report only - re-run with --apply to update config.json, then remember to add a DERATE_CHANGE_HISTORY entry and restart the service.)")
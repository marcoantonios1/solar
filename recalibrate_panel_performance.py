"""
Recalibrates the temporary panel-performance derate factor (config.json ->
panels.temporary_performance_derate) from real, accumulated data - a
reusable, re-runnable version of the one-off analysis done 2026-08-13.

Reports the current vs recommended value. Never auto-applies silently -
this is a safety-relevant parameter (feeds into every layer's decision),
so a human reviews the number before it changes anything live. Run this
periodically (e.g. monthly, or after cleaning the panels) to keep the
derate honest as real performance changes over time.

The change history lives in config.json (panels.derate_change_history),
not a separate Python constant - real bug found live 2026-08-13: keeping
it in a docstring-instruction-maintained constant let it drift out of
sync with the actual applied value within minutes. --apply now updates
both the factor and its history entry in the same atomic write.

Usage:
    python3 recalibrate_panel_performance.py           # report only
    python3 recalibrate_panel_performance.py --apply    # also updates config.json
"""
import sqlite3
import json
import argparse
from datetime import datetime

from config_loader import config

DB_PATH = config["database"]["path"]
MIN_EXPECTED_POWER_FOR_COMPARISON = config["performance_monitoring"]["min_expected_power_for_comparison_w"]


def normalize_expected(timestamp, expected, history=None):
    """Divides out whatever derate factor was active at this reading's time, reconstructing the raw model's original prediction."""
    if history is None:
        history = config["panels"].get("derate_change_history", [])
    active_factor = 1.0
    for change in history:
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

    history = config["panels"].get("derate_change_history", [])
    gaps = []
    for timestamp, actual, expected in rows:
        normalized_expected = normalize_expected(timestamp, expected, history)
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
    parser.add_argument("--apply", action="store_true", help="Write the recommended value AND a new history entry to config.json")
    args = parser.parse_args()

    recommended = recalibrate()

    if recommended is not None and args.apply:
        with open("config.json") as f:
            cfg = json.load(f)

        cfg["panels"]["temporary_performance_derate"] = recommended
        cfg["panels"].setdefault("derate_change_history", []).append({
            "effective_from": datetime.now().isoformat(timespec="seconds"),
            "factor": recommended,
        })

        with open("config.json", "w") as f:
            json.dump(cfg, f, indent=2)

        print(f"\nconfig.json updated: factor set to {recommended}, history entry appended automatically.")
        print("Remember to restart edl-solar.service for the live system to pick this up.")
    elif recommended is not None:
        print("\n(Report only - re-run with --apply to update config.json.)")
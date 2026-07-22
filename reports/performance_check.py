import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3
from datetime import datetime, timedelta

from config_loader import config

DB_PATH = config["database"]["path"]

MIN_EXPECTED_POWER_FOR_COMPARISON = 200  # W - below this, % gap is too noisy (twilight/low-angle artifacts)
MAX_CLOUD_COVER_FOR_COMPARISON = 20      # % - above this, clear-sky model isn't a fair baseline
UNDERPERFORMANCE_THRESHOLD_PCT = -15     # flag if actual is this much below expected
SUSTAINED_COUNT = 5                      # consecutive qualifying readings needed to flag


def check_performance(hours):
    conn = sqlite3.connect(DB_PATH)
    end = datetime.now()
    start = end - timedelta(hours=hours)
    start_str = start.isoformat(timespec="seconds")
    end_str = end.isoformat(timespec="seconds")

    rows = conn.execute(
        """SELECT timestamp, pv_power, expected_pv_power, cloud_cover FROM readings
           WHERE timestamp >= ? AND timestamp <= ?
           AND expected_pv_power IS NOT NULL
           AND expected_pv_power >= ?
           AND cloud_cover IS NOT NULL
           AND cloud_cover <= ?
           ORDER BY timestamp ASC""",
        (start_str, end_str, MIN_EXPECTED_POWER_FOR_COMPARISON, MAX_CLOUD_COVER_FOR_COMPARISON)
    ).fetchall()

    if not rows:
        print(f"No qualifying readings in the last {hours} hours "
              f"(need expected_pv_power >= {MIN_EXPECTED_POWER_FOR_COMPARISON}W "
              f"and cloud_cover <= {MAX_CLOUD_COVER_FOR_COMPARISON}%).")
        return

    print(f"\n{'Timestamp':<22} {'Actual (W)':>12} {'Expected (W)':>14} {'Cloud %':>9} {'Gap (%)':>10}")
    print("-" * 72)

    gaps = []
    consecutive_underperform = 0
    max_consecutive_underperform = 0
    flagged = False

    for timestamp, actual, expected, cloud in rows:
        gap_pct = ((actual - expected) / expected) * 100
        gaps.append(gap_pct)
        print(f"{timestamp:<22} {actual:>12.1f} {expected:>14.1f} {cloud:>8.0f}% {gap_pct:>9.1f}%")

        if gap_pct <= UNDERPERFORMANCE_THRESHOLD_PCT:
            consecutive_underperform += 1
            max_consecutive_underperform = max(max_consecutive_underperform, consecutive_underperform)
        else:
            consecutive_underperform = 0

        if consecutive_underperform >= SUSTAINED_COUNT:
            flagged = True

    avg_gap = sum(gaps) / len(gaps)
    print("-" * 72)
    print(f"Average gap across {len(gaps)} qualifying readings: {avg_gap:.1f}%")
    print(f"(Negative = underperforming vs. model, positive = outperforming)")
    print(f"Longest streak of readings at or below {UNDERPERFORMANCE_THRESHOLD_PCT}%: {max_consecutive_underperform}")

    print()
    if flagged:
        print(f"*** FLAG: sustained underperformance detected ***")
        print(f"Actual output was {UNDERPERFORMANCE_THRESHOLD_PCT}% or more below expected for "
              f"{max_consecutive_underperform} consecutive qualifying readings (clear sky, good sun angle).")
        print(f"Worth investigating: dirty panels, developing shading, or a wiring/MPPT issue.")
    else:
        print(f"No sustained underperformance flagged (threshold: {SUSTAINED_COUNT}+ consecutive "
              f"readings at or below {UNDERPERFORMANCE_THRESHOLD_PCT}%).")

    conn.close()


if __name__ == "__main__":
    hours = 24
    if len(sys.argv) > 1:
        try:
            hours = int(sys.argv[1])
        except ValueError:
            print(f"Invalid hours argument '{sys.argv[1]}', defaulting to 24.")
    check_performance(hours)
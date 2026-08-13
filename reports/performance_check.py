import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3
from datetime import datetime, timedelta

from config_loader import config

DB_PATH = config["database"]["path"]

MIN_EXPECTED_POWER_FOR_COMPARISON = config["performance_monitoring"]["min_expected_power_for_comparison_w"]
BUCKET_MINUTES = config["performance_monitoring"]["bucket_minutes"]
UNDERPERFORMANCE_THRESHOLD_PCT = config["performance_monitoring"]["underperformance_threshold_pct"]
SUSTAINED_BUCKETS = config["performance_monitoring"]["sustained_buckets"] 


def check_performance(hours):
    conn = sqlite3.connect(DB_PATH)
    end = datetime.now()
    start = end - timedelta(hours=hours)
    start_str = start.isoformat(timespec="seconds")
    end_str = end.isoformat(timespec="seconds")

    rows = conn.execute(
        """SELECT timestamp, pv_power, expected_pv_power_weather_raw, ambient_temp_c FROM readings
           WHERE timestamp >= ? AND timestamp <= ?
           AND expected_pv_power_weather_raw IS NOT NULL
           AND expected_pv_power_weather_raw >= ?
           ORDER BY timestamp ASC""",
        (start_str, end_str, MIN_EXPECTED_POWER_FOR_COMPARISON)
    ).fetchall()

    if not rows:
        print(f"No qualifying readings in the last {hours} hours "
              f"(need expected_pv_power_weather_raw >= {MIN_EXPECTED_POWER_FOR_COMPARISON}W).")
        return

    # Bucket readings into BUCKET_MINUTES windows and average actual/expected within each
    buckets = {}
    for timestamp, actual, expected, temp in rows:
        dt = datetime.fromisoformat(timestamp)
        bucket_key = dt.replace(
            minute=(dt.minute // BUCKET_MINUTES) * BUCKET_MINUTES,
            second=0, microsecond=0
        )
        if bucket_key not in buckets:
            buckets[bucket_key] = {"actual": [], "expected": [], "temp": []}
        buckets[bucket_key]["actual"].append(actual)
        buckets[bucket_key]["expected"].append(expected)
        if temp is not None:
            buckets[bucket_key]["temp"].append(temp)

    print(f"\n{'Bucket':<20} {'Actual (W)':>12} {'Expected (W)':>14} {'Gap (%)':>10} {'Temp (C)':>10} {'N':>4}")
    print("-" * 76)

    gaps = []
    consecutive_underperform = 0
    max_consecutive_underperform = 0
    flagged = False

    for bucket_key in sorted(buckets.keys()):
        actual_avg = sum(buckets[bucket_key]["actual"]) / len(buckets[bucket_key]["actual"])
        expected_avg = sum(buckets[bucket_key]["expected"]) / len(buckets[bucket_key]["expected"])
        temp_list = buckets[bucket_key]["temp"]
        temp_avg = sum(temp_list) / len(temp_list) if temp_list else None
        n = len(buckets[bucket_key]["actual"])

        gap_pct = ((actual_avg - expected_avg) / expected_avg) * 100
        gaps.append(gap_pct)
        temp_str = f"{temp_avg:.1f}" if temp_avg is not None else "N/A"
        print(f"{bucket_key.isoformat(timespec='minutes'):<20} {actual_avg:>12.1f} {expected_avg:>14.1f} {gap_pct:>9.1f}% {temp_str:>10} {n:>4}")

        if gap_pct <= UNDERPERFORMANCE_THRESHOLD_PCT:
            consecutive_underperform += 1
            max_consecutive_underperform = max(max_consecutive_underperform, consecutive_underperform)
        else:
            consecutive_underperform = 0

        if consecutive_underperform >= SUSTAINED_BUCKETS:
            flagged = True

    avg_gap = sum(gaps) / len(gaps)
    print("-" * 66)
    print(f"Average gap across {len(gaps)} buckets ({BUCKET_MINUTES}-min averages): {avg_gap:.1f}%")
    print(f"(Negative = underperforming vs. model, positive = outperforming)")
    print(f"Longest streak of buckets at or below {UNDERPERFORMANCE_THRESHOLD_PCT}%: {max_consecutive_underperform}")

    print()
    if flagged:
        print(f"*** FLAG: sustained underperformance detected ***")
        print(f"Actual output was {UNDERPERFORMANCE_THRESHOLD_PCT}% or more below expected for "
              f"{max_consecutive_underperform} consecutive {BUCKET_MINUTES}-min buckets.")
        print(f"Worth investigating: dirty panels, developing shading, or a wiring/MPPT issue.")
    else:
        print(f"No sustained underperformance flagged (threshold: {SUSTAINED_BUCKETS}+ consecutive "
              f"{BUCKET_MINUTES}-min buckets at or below {UNDERPERFORMANCE_THRESHOLD_PCT}%).")

    conn.close()


if __name__ == "__main__":
    hours = 24
    if len(sys.argv) > 1:
        try:
            hours = int(sys.argv[1])
        except ValueError:
            print(f"Invalid hours argument '{sys.argv[1]}', defaulting to 24.")
    check_performance(hours)
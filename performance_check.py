import sqlite3
import sys
from datetime import datetime, timedelta

from config_loader import config

DB_PATH = config["database"]["path"]

MIN_EXPECTED_POWER_FOR_COMPARISON = 200  # W - below this, % gap is too noisy to be meaningful


def check_performance(hours):
    conn = sqlite3.connect(DB_PATH)
    end = datetime.now()
    start = end - timedelta(hours=hours)
    start_str = start.isoformat(timespec="seconds")
    end_str = end.isoformat(timespec="seconds")

    rows = conn.execute(
        """SELECT timestamp, pv_power, expected_pv_power FROM readings
           WHERE timestamp >= ? AND timestamp <= ?
           AND expected_pv_power IS NOT NULL
           AND expected_pv_power >= ?
           ORDER BY timestamp ASC""",
        (start_str, end_str, MIN_EXPECTED_POWER_FOR_COMPARISON)
    ).fetchall()

    if not rows:
        print(f"No readings with expected_pv_power >= {MIN_EXPECTED_POWER_FOR_COMPARISON}W "
              f"in the last {hours} hours. (Likely no strong-sun readings in this window yet.)")
        return

    print(f"\n{'Timestamp':<22} {'Actual (W)':>12} {'Expected (W)':>14} {'Gap (%)':>10}")
    print("-" * 62)

    gaps = []
    for timestamp, actual, expected in rows:
        gap_pct = ((actual - expected) / expected) * 100
        gaps.append(gap_pct)
        print(f"{timestamp:<22} {actual:>12.1f} {expected:>14.1f} {gap_pct:>9.1f}%")

    avg_gap = sum(gaps) / len(gaps)
    print("-" * 62)
    print(f"Average gap across {len(gaps)} readings: {avg_gap:.1f}%")
    print(f"(Negative = underperforming vs. model, positive = outperforming)")

    conn.close()


if __name__ == "__main__":
    hours = 24
    if len(sys.argv) > 1:
        try:
            hours = int(sys.argv[1])
        except ValueError:
            print(f"Invalid hours argument '{sys.argv[1]}', defaulting to 24.")
    check_performance(hours)
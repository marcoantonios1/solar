import sqlite3
from datetime import datetime

from config_loader import config

DB_PATH = config["database"]["path"]
CAPACITY_KWH_USABLE = config["battery"]["capacity_kwh_usable"]
POLL_INTERVAL_SECONDS_APPROX = config["polling"]["interval_seconds"]


def seed_prior_cycles(conn, true_cycle_count_as_of_now):
    """
    Sets the battery's TRUE lifetime cycle count as of RIGHT NOW, from an
    authoritative source (e.g. the inverter's own BMS display) - and resets
    our own tracking to start counting only from this exact checkpoint
    forward. This avoids double-counting: our own load/solar-derived
    estimate is only ever asked to account for NEW cycles after this point,
    never to reconstruct history a trusted source already knows precisely.
    """
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """UPDATE battery_cumulative_stats
           SET seeded_prior_cycles = ?, cumulative_cycles = 0, last_calculated_through = ?
           WHERE id = 1""",
        (true_cycle_count_as_of_now, now)
    )
    conn.commit()


def update_cumulative_cycles(conn, dry_run=False):
    """
    Incrementally adds NEW cycle-equivalents since the last update, rather
    than recalculating from scratch every time - cheap to run regularly
    (e.g. monthly, alongside the report) even as history grows for years.

    Issue #125 fixed: previously, net = load - pv - ac_charge attributed
    ANY deficit to battery discharge - but whenever EDL is present, it may
    be powering the house directly (UTI modes), not discharging the
    battery at all. readings doesn't store output_priority directly, so
    this uses the safer approximation of zeroing counted discharge
    whenever edl_present=1 (slightly undercounts the rarer EDL-present-
    but-blocked case, which is a far safer error than the confirmed
    overcounting bug it replaces). Also caps each step's time interval at
    2x the polling interval, so a real outage/gap isn't integrated as if
    the last known power level ran continuously the whole time.

    Issue #126 fixed: dry_run=True computes the same real numbers without
    writing anything - so a test/preview render can't silently advance
    last_calculated_through and corrupt the next real report's
    "new cycles this period" figure.
    """
    row = conn.execute(
        "SELECT cumulative_cycles, last_calculated_through FROM battery_cumulative_stats WHERE id = 1"
    ).fetchone()
    cumulative_cycles, last_calculated_through = row

    start = last_calculated_through or "2000-01-01T00:00:00"  # if never run, use all history
    end = datetime.now().isoformat(timespec="seconds")

    readings = conn.execute(
        """SELECT timestamp, pv_power, load_power, ac_charge_power, edl_present FROM readings
           WHERE timestamp > ? AND timestamp <= ?
           AND pv_power IS NOT NULL AND load_power IS NOT NULL
           ORDER BY timestamp ASC""",
        (start, end)
    ).fetchall()

    max_step_hours = (2 * POLL_INTERVAL_SECONDS_APPROX) / 3600

    discharge_kwh = 0.0
    for i in range(len(readings) - 1):
        t1, pv1, load1, ac1, edl1 = readings[i]
        t2, pv2, load2, ac2, edl2 = readings[i + 1]

        if edl1 or edl2:
            continue  # EDL present for this interval - may be powering the house directly, don't attribute to battery discharge

        net1 = load1 - pv1 - (ac1 or 0)
        net2 = load2 - pv2 - (ac2 or 0)
        avg_net = (net1 + net2) / 2
        if avg_net > 0:
            dt1 = datetime.fromisoformat(t1)
            dt2 = datetime.fromisoformat(t2)
            hours = min((dt2 - dt1).total_seconds() / 3600, max_step_hours)
            discharge_kwh += (avg_net * hours) / 1000

    new_cycles = discharge_kwh / CAPACITY_KWH_USABLE
    updated_total = cumulative_cycles + new_cycles

    if not dry_run:
        conn.execute(
            "UPDATE battery_cumulative_stats SET cumulative_cycles = ?, last_calculated_through = ? WHERE id = 1",
            (updated_total, end)
        )
        conn.commit()

    return {
        "new_cycles_this_period": round(new_cycles, 3),
        "cumulative_cycles_since_logging": round(updated_total, 2),
        "dry_run": dry_run,
    }


def get_lifetime_cycle_estimate(conn):
    """Returns the full lifetime estimate: seeded prior + everything tracked since."""
    row = conn.execute(
        "SELECT seeded_prior_cycles, cumulative_cycles FROM battery_cumulative_stats WHERE id = 1"
    ).fetchone()
    seeded, cumulative = row
    return {
        "seeded_prior_cycles": seeded,
        "cumulative_cycles_since_logging": round(cumulative, 2),
        "estimated_lifetime_cycles": round(seeded + cumulative, 2),
    }
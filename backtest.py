"""
Backtesting harness (Issue #135): replays historical daily_predictions
against ALTERNATIVE thresholds, without touching the live system, so
threshold tuning (Issue #90) can be done against real evidence instead
of manual guessing. Scores each candidate threshold pair by how many
historical days would have classified differently, and uses that day's
REAL EDL usage as a proxy for the practical cost/risk of the change.

This replays the BASIC tier only (classify_energy_balance) - not the
full decide_target_state() wrapper (recharge-shortfall/tomorrow-lookahead
escalations), which depend on additional daily context beyond what's
stored per-row. A reasonable, honest first scope, not a full simulation.
"""
import sqlite3
import argparse

import output_mode_manager as omm
from output_mode_manager import classify_energy_balance


def backtest_thresholds(conn, charge_needed_threshold_kwh, shortfall_threshold_kwh, start=None, end=None):
    query = """SELECT date, solar_expected_kwh, house_expected_kwh, battery_available_kwh,
                      decision_label, charger_mode, output_priority
               FROM daily_predictions"""
    params = []
    if start and end:
        query += " WHERE date >= ? AND date <= ?"
        params = [start, end]
    query += " ORDER BY date ASC"

    rows = conn.execute(query, params).fetchall()

    original_charge_needed = omm.CHARGE_NEEDED_THRESHOLD_KWH
    original_shortfall = omm.SHORTFALL_THRESHOLD_KWH

    results = []
    try:
        omm.CHARGE_NEEDED_THRESHOLD_KWH = charge_needed_threshold_kwh
        omm.SHORTFALL_THRESHOLD_KWH = shortfall_threshold_kwh

        for date, solar, house, battery, actual_label, actual_charger, actual_output in rows:
            alt_charger, alt_output, alt_label, balance_kwh = classify_energy_balance(
                solar_expected_kwh=solar, battery_available_kwh=battery, house_expected_kwh=house
            )

            from inverter import mode_name
            changed = mode_name(alt_charger) != actual_charger or str(alt_output) != actual_output

            day_start, day_end = date + "T00:00:00", date + "T23:59:59"
            edl_kwh, edl_cost = conn.execute(
                """SELECT COALESCE(SUM(total_kwh_charged_during), 0), COALESCE(SUM(cost_usd), 0)
                   FROM edl_events WHERE start_time >= ? AND start_time <= ?""",
                (day_start, day_end)
            ).fetchone()

            results.append({
                "date": date, "balance_kwh": round(balance_kwh, 2),
                "actual_label": actual_label, "alt_label": alt_label, "changed": changed,
                "real_edl_kwh_that_day": round(edl_kwh, 2), "real_edl_cost_that_day": round(edl_cost, 4),
            })
    finally:
        omm.CHARGE_NEEDED_THRESHOLD_KWH = original_charge_needed
        omm.SHORTFALL_THRESHOLD_KWH = original_shortfall

    changed_days = [r for r in results if r["changed"]]
    return {
        "charge_needed_threshold_kwh": charge_needed_threshold_kwh,
        "shortfall_threshold_kwh": shortfall_threshold_kwh,
        "total_days": len(results),
        "days_changed": len(changed_days),
        "edl_cost_on_changed_days": round(sum(r["real_edl_cost_that_day"] for r in changed_days), 4),
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest alternative classify_energy_balance() thresholds against real history")
    parser.add_argument("--charge-needed", type=float, required=True, help="Alternative CHARGE_NEEDED_THRESHOLD_KWH to test")
    parser.add_argument("--shortfall", type=float, required=True, help="Alternative SHORTFALL_THRESHOLD_KWH to test")
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--end", type=str, default=None)
    args = parser.parse_args()

    from config_loader import config
    conn = sqlite3.connect(config["database"]["path"])

    result = backtest_thresholds(conn, args.charge_needed, args.shortfall, args.start, args.end)
    print(f"Thresholds tested: charge_needed={args.charge_needed}, shortfall={args.shortfall}")
    print(f"Total days: {result['total_days']}, days that would classify differently: {result['days_changed']}")
    print(f"Real EDL cost incurred on changed days: ${result['edl_cost_on_changed_days']}")
    print()
    for r in result["results"]:
        if r["changed"]:
            print(f"  {r['date']}: actual='{r['actual_label']}' -> alt='{r['alt_label']}' (real EDL that day: {r['real_edl_kwh_that_day']} kWh, ${r['real_edl_cost_that_day']})")
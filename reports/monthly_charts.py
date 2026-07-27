import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from reports.monthly_data import integrate_power_kwh
from reports.report import categorize_reason


def generate_edl_bar_chart(daily_breakdown, output_path):
    dates = [d["date"][5:] for d in daily_breakdown]
    edl_kwh = [d["edl_kwh"] for d in daily_breakdown]

    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.bar(dates, edl_kwh, color="#d97706")
    ax.set_ylabel("EDL kWh")
    ax.set_title("Daily EDL Energy Delivered")
    ax.tick_params(axis='x', rotation=45)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def generate_solar_line_chart(daily_breakdown, output_path):
    dates = [d["date"][5:] for d in daily_breakdown]
    solar_kwh = [d["solar_kwh"] for d in daily_breakdown]

    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(dates, solar_kwh, marker="o", color="#059669", linewidth=2)
    ax.set_ylabel("Solar kWh")
    ax.set_title("Daily Solar Output")
    ax.tick_params(axis='x', rotation=45)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def get_daily_expected_solar(conn, start, end):
    """Per-day expected solar kWh (weather-adjusted model), same date range as daily_breakdown."""
    start_date = datetime.fromisoformat(start).date()
    end_date = datetime.fromisoformat(end).date()

    days = []
    current = start_date
    while current <= end_date:
        day_start = f"{current.isoformat()}T00:00:00"
        day_end = f"{current.isoformat()}T23:59:59"
        expected_kwh = integrate_power_kwh(conn, "expected_pv_power_weather", day_start, day_end)
        days.append({"date": current.isoformat(), "expected_kwh": expected_kwh})
        current += timedelta(days=1)

    return days


def generate_expected_vs_actual_chart(daily_breakdown, daily_expected, output_path):
    dates = [d["date"][5:] for d in daily_breakdown]
    actual = [d["solar_kwh"] for d in daily_breakdown]
    expected = [d["expected_kwh"] for d in daily_expected]

    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(dates, expected, marker="o", color="#94a3b8", linewidth=2, linestyle="--", label="Expected")
    ax.plot(dates, actual, marker="o", color="#059669", linewidth=2, label="Actual")
    ax.set_ylabel("Solar kWh")
    ax.set_title("Daily Solar: Expected vs. Actual")
    ax.tick_params(axis='x', rotation=45)
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def get_daily_min_soc(conn, start, end):
    """Per-day lowest battery SOC reading."""
    start_date = datetime.fromisoformat(start).date()
    end_date = datetime.fromisoformat(end).date()

    days = []
    current = start_date
    while current <= end_date:
        day_start = f"{current.isoformat()}T00:00:00"
        day_end = f"{current.isoformat()}T23:59:59"
        row = conn.execute(
            "SELECT MIN(battery_soc) FROM readings WHERE timestamp >= ? AND timestamp <= ? AND battery_soc IS NOT NULL",
            (day_start, day_end)
        ).fetchone()
        days.append({"date": current.isoformat(), "min_soc": row[0]})
        current += timedelta(days=1)

    return days


def generate_daily_min_soc_chart(daily_min_soc, critical_floor, output_path):
    dates = [d["date"][5:] for d in daily_min_soc]
    min_soc = [d["min_soc"] if d["min_soc"] is not None else 0 for d in daily_min_soc]

    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(dates, min_soc, marker="o", color="#dc2626", linewidth=2)
    ax.axhline(y=critical_floor, color="#991b1b", linestyle="--", alpha=0.6, label=f"Critical floor ({critical_floor}%)")
    ax.set_ylabel("Lowest SOC (%)")
    ax.set_title("Daily Minimum Battery SOC")
    ax.tick_params(axis='x', rotation=45)
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def generate_house_load_chart(daily_breakdown, output_path):
    dates = [d["date"][5:] for d in daily_breakdown]
    house_kwh = [d["house_kwh"] for d in daily_breakdown]

    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.plot(dates, house_kwh, marker="o", color="#7c3aed", linewidth=2)
    ax.set_ylabel("House Load kWh")
    ax.set_title("Daily House Load")
    ax.tick_params(axis='x', rotation=45)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def get_edl_reason_breakdown(conn, start, end):
    """Sessions and kWh grouped by trigger-reason category, reusing report.py's categorize_reason."""
    events = conn.execute(
        """SELECT total_kwh_charged_during, reason FROM edl_events
           WHERE start_time >= ? AND start_time <= ? AND end_time IS NOT NULL""",
        (start, end)
    ).fetchall()

    breakdown = {}
    for kwh, reason in events:
        category = categorize_reason(reason)
        if category not in breakdown:
            breakdown[category] = {"sessions": 0, "kwh": 0.0}
        breakdown[category]["sessions"] += 1
        breakdown[category]["kwh"] += kwh or 0

    return breakdown


def generate_edl_reason_chart(reason_breakdown, output_path):
    if not reason_breakdown:
        return  # nothing to chart

    categories = list(reason_breakdown.keys())
    kwh_values = [reason_breakdown[c]["kwh"] for c in categories]

    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.barh(categories, kwh_values, color="#0369a1")
    ax.set_xlabel("EDL kWh")
    ax.set_title("EDL Usage by Trigger Reason")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    import sqlite3
    from config_loader import config
    from reports.monthly_data import get_full_monthly_report_data

    data = get_full_monthly_report_data(days=7)
    conn = sqlite3.connect(config["database"]["path"])

    generate_edl_bar_chart(data["daily_breakdown"], "/tmp/chart_edl_bar.png")
    generate_solar_line_chart(data["daily_breakdown"], "/tmp/chart_solar_line.png")

    daily_expected = get_daily_expected_solar(conn, data["period_start"], data["period_end"])
    generate_expected_vs_actual_chart(data["daily_breakdown"], daily_expected, "/tmp/chart_solar_expected_vs_actual.png")

    daily_min_soc = get_daily_min_soc(conn, data["period_start"], data["period_end"])
    generate_daily_min_soc_chart(daily_min_soc, config["thresholds"]["low_soc_threshold"], "/tmp/chart_min_soc.png")

    generate_house_load_chart(data["daily_breakdown"], "/tmp/chart_house_load.png")

    reason_breakdown = get_edl_reason_breakdown(conn, data["period_start"], data["period_end"])
    generate_edl_reason_chart(reason_breakdown, "/tmp/chart_edl_reasons.png")

    conn.close()
    print("All 6 charts saved to /tmp/chart_*.png")
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib
matplotlib.use("Agg")  # no display needed - just saving to file
import matplotlib.pyplot as plt


def generate_edl_bar_chart(daily_breakdown, output_path):
    """
    Bar chart of daily EDL kWh delivered across the report period.
    Saves a PNG to output_path.
    """
    dates = [d["date"][5:] for d in daily_breakdown]  # MM-DD, shorter labels
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
    """
    Line chart of daily solar kWh generated across the report period.
    Saves a PNG to output_path.
    """
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


if __name__ == "__main__":
    from reports.monthly_data import get_full_monthly_report_data

    data = get_full_monthly_report_data(days=7)
    generate_edl_bar_chart(data["daily_breakdown"], "/tmp/edl_chart_test.png")
    generate_solar_line_chart(data["daily_breakdown"], "/tmp/solar_chart_test.png")
    print("Charts saved to /tmp/edl_chart_test.png and /tmp/solar_chart_test.png")
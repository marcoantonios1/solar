import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import tempfile
from datetime import datetime
from battery_cumulative import update_cumulative_cycles, get_lifetime_cycle_estimate

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak, HRFlowable
)

from config_loader import config
from reports.monthly_data import get_full_monthly_report_data
from reports.monthly_charts import (
    generate_edl_bar_chart, generate_solar_line_chart,
    get_daily_expected_solar, generate_expected_vs_actual_chart,
    get_daily_min_soc, generate_daily_min_soc_chart,
    generate_house_load_chart,
    get_edl_reason_breakdown, generate_edl_reason_chart,
)

DB_PATH = config["database"]["path"]
CRITICAL_SOC_FLOOR = config["thresholds"]["low_soc_threshold"]

styles = getSampleStyleSheet()
title_style = ParagraphStyle('ReportTitle', parent=styles['Title'], fontSize=20, spaceAfter=6)
section_style = ParagraphStyle('Section', parent=styles['Heading1'], fontSize=14,
                                textColor=colors.HexColor("#0f172a"), spaceBefore=16, spaceAfter=8)
body_style = ParagraphStyle('Body', parent=styles['Normal'], fontSize=10, leading=14, spaceAfter=6)
small_style = ParagraphStyle('Small', parent=styles['Normal'], fontSize=8, textColor=colors.HexColor("#64748b"))


def fmt(val, suffix="", none_text="N/A"):
    if val is None:
        return none_text
    return f"{val}{suffix}"


def build_monthly_pdf(days=None, start_str=None, end_str=None, output_path="monthly_report.pdf", dry_run=False):
    data = get_full_monthly_report_data(days=days, start_str=start_str, end_str=end_str)
    conn = sqlite3.connect(DB_PATH)
    cycle_update = update_cumulative_cycles(conn, dry_run=dry_run)
    lifetime_stats = get_lifetime_cycle_estimate(conn)

    doc = SimpleDocTemplate(output_path, pagesize=letter,
                             topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                             leftMargin=0.7 * inch, rightMargin=0.7 * inch)
    story = []

    period_label = f"{data['period_start'][:10]} to {data['period_end'][:10]} ({data['period_days']} days)"

    story.append(Paragraph("EDL Solar Automation — Monthly Report", title_style))
    story.append(Paragraph(period_label, body_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cbd5e1")))

    # 1. Executive Summary
    es = data["executive_summary"]
    story.append(Paragraph("1. Executive Summary", section_style))
    longest = es["longest_edl_event"]
    longest_str = "No EDL sessions this period."
    if longest:
        longest_str = f"Longest EDL session: {longest[3]:.1f} min ({longest[1][:16]} to {longest[2][:16]}), cost ${longest[4]:.4f}"
    story.append(Paragraph(
        f"Total EDL availability: <b>{fmt(es['edl_available_hours'], ' hours')}</b> this period<br/>"
        f"Total solar generated: <b>{fmt(es['total_solar_kwh'], ' kWh')}</b><br/>"
        f"Total EDL cost: <b>${fmt(es['total_edl_cost'])}</b><br/>"
        f"Estimated cost under old always-on EDL behavior: ${fmt(es['old_way_cost_estimate'])}<br/>"
        f"Estimated savings: <b>${fmt(es['estimated_savings'])}</b><br/>"
        f"{longest_str}",
        body_style
    ))

    # 2. Monthly Totals
    mt = data["monthly_totals"]
    story.append(Paragraph("2. Monthly Totals", section_style))
    totals_table = Table([
        ["Metric", "Value"],
        ["Total house kWh used", fmt(mt["total_house_kwh"], " kWh")],
        ["Total solar kWh generated", fmt(mt["total_solar_kwh"], " kWh")],
        ["Total EDL sessions", str(mt["total_edl_sessions"])],
        ["  — charged", str(mt["edl_sessions_charged"])],
        ["  — present but blocked", str(mt["edl_sessions_blocked"])],
        ["Total EDL kWh delivered", fmt(mt["total_edl_kwh"], " kWh")],
        ["Total EDL cost", f"${fmt(mt['total_edl_cost'])}"],
    ], colWidths=[3.2 * inch, 2.5 * inch])
    totals_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(totals_table)

    story.append(PageBreak())

    # 3. Daily Breakdown Table
    cell_style = ParagraphStyle('Cell', parent=styles['Normal'], fontSize=7, leading=9)
    header_cell_style = ParagraphStyle('HeaderCell', parent=styles['Normal'], fontSize=7, leading=9,
                                     textColor=colors.white, fontName='Helvetica-Bold')
    story.append(Paragraph("3. Daily Breakdown", section_style))
    daily_rows = [[
        Paragraph("Date", header_cell_style),
        Paragraph("Solar kWh", header_cell_style),
        Paragraph("House kWh", header_cell_style),
        Paragraph("EDL Sessions (chg/blk)", header_cell_style),
        Paragraph("EDL kWh", header_cell_style),
        Paragraph("EDL Cost", header_cell_style),
        Paragraph("Session Times", header_cell_style),
    ]]
    for d in data["daily_breakdown"]:
        times_str = ", ".join(f"{s[11:16]}-{e[11:16]}" for s, e in d["edl_session_times"] if s and e) or "—"
        daily_rows.append([
            d["date"], f"{d['solar_kwh']:.1f}", f"{d['house_kwh']:.1f}",
            f"{d['edl_sessions_charged']}/{d['edl_sessions_blocked']}",
            f"{d['edl_kwh']:.2f}", f"${d['edl_cost']:.4f}",
            Paragraph(times_str, cell_style)
        ])
    daily_table = Table(daily_rows, colWidths=[0.85 * inch, 0.7 * inch, 0.7 * inch, 0.95 * inch, 0.6 * inch, 0.65 * inch, 2.05 * inch])
    daily_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(daily_table)

    story.append(PageBreak())

    # 4. Solar Performance
    sp = data["solar_performance"]
    story.append(Paragraph("4. Solar Performance", section_style))
    story.append(Paragraph(
        f"Average expected-vs-actual gap (all conditions): <b>{fmt(sp['avg_gap_pct'], '%')}</b><br/>"
        f"Average gap, clear-sky readings only: <b>{fmt(sp['avg_gap_pct_clear_sky'], '%')}</b><br/>"
        f"Sustained underperformance episodes flagged: {fmt(sp['underperformance_flag_episodes'])}<br/>"
        f"Average cloud cover: {fmt(sp['avg_cloud_cover'], '%')}<br/>"
        f"Average ambient temperature: {fmt(sp['avg_ambient_temp_c'], '°C')}<br/>"
        f"Clear days: {fmt(sp['clear_days'])} / Cloudy days: {fmt(sp['cloudy_days'])}",
        body_style
    ))

    # 5. Battery Health
    bh = data["battery_health"]
    story.append(Paragraph("5. Battery Health", section_style))
    story.append(Paragraph(
        f"Lowest SOC reached this period: <b>{fmt(bh['lowest_soc_pct'], '%')}</b><br/>"
        f"Hours spent near critical floor: {fmt(bh['hours_near_critical_floor'], ' hrs')}<br/>"
        f"Rough cycle estimate (this period only): {fmt(bh['rough_cycle_estimate'])}<br/>"
        f"<br/>"
        f"<b>Cumulative (lifetime) tracking:</b><br/>"
        f"New cycles this update: {fmt(cycle_update['new_cycles_this_period'])}<br/>"
        f"Estimated lifetime cycles: <b>{fmt(lifetime_stats['estimated_lifetime_cycles'])}</b> "
        f"({fmt(lifetime_stats['seeded_prior_cycles'])} seeded from BMS + {fmt(lifetime_stats['cumulative_cycles_since_logging'])} tracked since)<br/>"
        f"<br/>"
        f"<i>{bh['note']}</i>",
        body_style
    ))

    # 6. System Health
    sh = data["system_health"]
    story.append(Paragraph("6. System Health / Reliability", section_style))
    story.append(Paragraph(
        f"Modbus read failures: {fmt(sh['modbus_read_failures'])}<br/>"
        f"Modbus reconnects: {fmt(sh['modbus_reconnects'])}<br/>"
        f"Unhandled crashes: {fmt(sh['unhandled_crashes'])}<br/>"
        f"Time in MANUAL_MODE: {fmt(sh['manual_mode_hours'], ' hrs') if sh['manual_mode_tracked'] else 'Not tracked before this period'}<br/>"
        f"Longest gap in logging: {fmt(sh['longest_logging_gap_minutes'], ' min')} "
        f"{'(large gap — some totals in this report may be incomplete)' if sh['longest_logging_gap_minutes'] > 60 else ''}",
        body_style
    ))

    story.append(PageBreak())

    # 7. Charts
    story.append(Paragraph("7. Charts", section_style))

    with tempfile.TemporaryDirectory() as tmpdir:
        chart_paths = {
            "edl_bar": os.path.join(tmpdir, "edl_bar.png"),
            "solar_line": os.path.join(tmpdir, "solar_line.png"),
            "expected_vs_actual": os.path.join(tmpdir, "expected_vs_actual.png"),
            "min_soc": os.path.join(tmpdir, "min_soc.png"),
            "house_load": os.path.join(tmpdir, "house_load.png"),
            "edl_reasons": os.path.join(tmpdir, "edl_reasons.png"),
        }

        generate_edl_bar_chart(data["daily_breakdown"], chart_paths["edl_bar"])
        generate_solar_line_chart(data["daily_breakdown"], chart_paths["solar_line"])

        daily_expected = get_daily_expected_solar(conn, data["period_start"], data["period_end"])
        generate_expected_vs_actual_chart(data["daily_breakdown"], daily_expected, chart_paths["expected_vs_actual"])

        daily_min_soc = get_daily_min_soc(conn, data["period_start"], data["period_end"])
        generate_daily_min_soc_chart(daily_min_soc, CRITICAL_SOC_FLOOR, chart_paths["min_soc"])

        generate_house_load_chart(data["daily_breakdown"], chart_paths["house_load"])

        reason_breakdown = get_edl_reason_breakdown(conn, data["period_start"], data["period_end"])
        generate_edl_reason_chart(reason_breakdown, chart_paths["edl_reasons"])

        for key in ["edl_bar", "solar_line", "expected_vs_actual", "min_soc", "house_load"]:
            story.append(Image(chart_paths[key], width=6.5 * inch, height=2.5 * inch))
            story.append(Spacer(1, 12))

        if os.path.exists(chart_paths["edl_reasons"]):
            story.append(Image(chart_paths["edl_reasons"], width=6.5 * inch, height=2.5 * inch))

        doc.build(story)

    conn.close()
    print(f"Report saved to {output_path}")


if __name__ == "__main__":
    build_monthly_pdf(days=7, output_path="monthly_report_test.pdf", dry_run=True)
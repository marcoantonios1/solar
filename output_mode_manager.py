import pandas as pd

from inverter import (
    SBU, UTI, SNU, OSO,
)
from energy_balance import calculate_energy_balance as _calculate_energy_balance
from config_loader import config
from proposal import Proposal

SHORTFALL_THRESHOLD_KWH = config["thresholds"]["shortfall_threshold_kwh"]
CHARGE_NEEDED_THRESHOLD_KWH = config["thresholds"]["charge_needed_threshold_kwh"]
TOMORROW_SHORTFALL_LOOKAHEAD_KWH = config["thresholds"]["tomorrow_shortfall_lookahead_kwh"]
TARIFF_AWARE_MIN_FACTOR = config["thresholds"].get("tariff_aware_min_factor", 1.0)
TARIFF_AWARE_MAX_FACTOR = config["thresholds"].get("tariff_aware_max_factor", 1.0)


def get_tariff_adjusted_lookahead_threshold(conn):
    """
    Tier-1 EDL allowance (100 kWh/month at $0.10/kWh) is otherwise tracked
    for accounting only. Near month-start with allowance remaining,
    marginal charging is near-free insurance ($0.10/kWh); deep in tier 2
    it's $0.27/kWh - a real cost difference worth reflecting in how eager
    the tomorrow-lookahead check is to proactively escalate. Scales
    linearly: full tier-1 remaining -> more generous (readier to act on
    a predicted shortfall); tier-1 exhausted -> stricter (requires a
    bigger, more certain shortfall before committing to expensive EDL).
    """
    tier1_limit = config["edl_tariff"]["tier1_limit_kwh"]
    today_str = pd.Timestamp.now(tz="Asia/Beirut").strftime("%Y-%m-%d")
    month_start = today_str[:7] + "-01T00:00:00"

    used_this_month = conn.execute(
        "SELECT COALESCE(SUM(total_kwh_charged_during), 0) FROM edl_events WHERE start_time >= ?",
        (month_start,)
    ).fetchone()[0]

    remaining_fraction = max(0.0, min(1.0, (tier1_limit - used_this_month) / tier1_limit))
    factor = TARIFF_AWARE_MIN_FACTOR + (remaining_fraction * (TARIFF_AWARE_MAX_FACTOR - TARIFF_AWARE_MIN_FACTOR))

    return TOMORROW_SHORTFALL_LOOKAHEAD_KWH * factor


def classify_energy_balance(solar_expected_kwh, battery_available_kwh, house_expected_kwh):
    """
    Core, reusable three-tier decision - shared by Layer 1 (daily), Layer 2
    (hourly), and relax. Delegates to calculate_energy_balance() for the
    actual number (Issue #176-review bug 1) - this function ONLY maps that
    number to a tier. There is now exactly ONE place computing the balance,
    not two that could silently drift apart.
    """
    balance = _calculate_energy_balance(solar_expected_kwh, battery_available_kwh, house_expected_kwh)
    balance_kwh = balance["balance_kwh"]

    if balance_kwh < CHARGE_NEEDED_THRESHOLD_KWH:
        return SNU, UTI, "shortfall - charge + power house (SNU+UTI)", balance_kwh
    elif balance_kwh < SHORTFALL_THRESHOLD_KWH:
        return OSO, UTI, "small deficit - power house only, spare battery (OSO+UTI)", balance_kwh
    else:
        return OSO, SBU, "surplus - default, minimize EDL (OSO+SBU)", balance_kwh


def decide_target_state(predictions, conn=None):
    """
    Returns a Proposal based on today's basic tier (via classify_energy_balance),
    escalated further if either the battery-recharge check or tomorrow's
    forecast warrants it - these can only push the decision UP toward
    SNU+UTI, never relax a tier that classify_energy_balance already
    escalated to.
    """
    today = predictions[0]

    charger_mode, output_priority, label, balance_kwh = classify_energy_balance(
        solar_expected_kwh=today["solar_expected_kwh"],
        battery_available_kwh=today["battery_available_kwh"],
        house_expected_kwh=today["house_expected_kwh"],
    )

    recharge = today.get("battery_recharge_status")
    recharge_shortfall = (
        recharge is not None and not recharge["will_reach_full"]
    )

    tomorrow_shortfall = False
    if len(predictions) > 1:
        tomorrow_balance = predictions[1]["balance_kwh"]
        effective_threshold = (
            get_tariff_adjusted_lookahead_threshold(conn) if conn is not None else TOMORROW_SHORTFALL_LOOKAHEAD_KWH
        )
        tomorrow_shortfall = tomorrow_balance < effective_threshold

    if charger_mode == SNU:
        final_charger, final_output, final_label = charger_mode, output_priority, label
    elif recharge_shortfall:
        final_charger, final_output, final_label = SNU, UTI, "shortfall - charge + power house (SNU+UTI)"
    elif tomorrow_shortfall:
        final_charger, final_output, final_label = SNU, UTI, f"tomorrow predicted shortfall ({predictions[1]['balance_kwh']} kWh) - proactively building buffer today (SNU+UTI)"
    else:
        final_charger, final_output, final_label = charger_mode, output_priority, label

    return Proposal(charger_mode=final_charger, output_priority=final_output, reason=final_label, source="layer1")
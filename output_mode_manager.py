from inverter import (
    SBU, UTI, SNU, OSO,
)
from energy_balance import calculate_energy_balance as _calculate_energy_balance
from config_loader import config
from proposal import Proposal
from actuator import apply_state

SHORTFALL_THRESHOLD_KWH = config["thresholds"]["shortfall_threshold_kwh"]
CHARGE_NEEDED_THRESHOLD_KWH = config["thresholds"]["charge_needed_threshold_kwh"]
TOMORROW_SHORTFALL_LOOKAHEAD_KWH = config["thresholds"]["tomorrow_shortfall_lookahead_kwh"]


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


def decide_target_state(predictions):
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
        tomorrow_shortfall = tomorrow_balance < TOMORROW_SHORTFALL_LOOKAHEAD_KWH

    if charger_mode == SNU:
        final_charger, final_output, final_label = charger_mode, output_priority, label
    elif recharge_shortfall:
        final_charger, final_output, final_label = SNU, UTI, "shortfall - charge + power house (SNU+UTI)"
    elif tomorrow_shortfall:
        final_charger, final_output, final_label = SNU, UTI, f"tomorrow predicted shortfall ({predictions[1]['balance_kwh']} kWh) - proactively building buffer today (SNU+UTI)"
    else:
        final_charger, final_output, final_label = charger_mode, output_priority, label

    return Proposal(charger_mode=final_charger, output_priority=final_output, reason=final_label, source="layer1")


def apply_output_mode_decision(client, conn, predictions):
    """
    Applies Layer 1's daily proposal using the shared actuator (Issue
    #149) - verified write-back, unconditional logging - instead of its
    own separate read/compare/write/log logic.
    """
    proposal = decide_target_state(predictions)

    result = apply_state(client, conn, proposal.charger_mode, proposal.output_priority, proposal.reason)

    print(f"Decision: {proposal.reason}")
    if result["action"] == "changed":
        print(f"Applied: charger={result['new_charger']}, output={result['new_output']}")
    elif result["action"] == "no_change":
        print("No change needed - already in target state.")
    else:
        print(f"Action: {result}")

    return proposal.charger_mode, proposal.output_priority, proposal.reason
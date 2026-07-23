from config_loader import config

BREAKER_LIMIT_A = config["breaker_safety"]["breaker_limit_a"]
SAFETY_MARGIN_A = config["breaker_safety"]["safety_margin_a"]
GRID_VOLTAGE_V = config["breaker_safety"]["grid_voltage_v"]
CONVERSION_EFFICIENCY = config["breaker_safety"]["conversion_efficiency"]

BATTERY_VOLTAGE = config["battery"]["nominal_voltage"]


def calculate_safe_charge_current(load_power_w):
    """
    Returns the max safe DC charge current (A, on the battery side - what
    Program 11 / MaxChargeCurr actually sets) given current house load,
    leaving room under the 20A AC smart breaker for both EDL charging
    (converted through the inverter) and EDL house-load, which draw from
    the same breaker simultaneously in UTI mode.

    MaxChargeCurr is a DC-side (battery-voltage) setting, NOT an AC-side
    amp value - these are two different voltage domains and must be
    converted through power (watts), not compared directly as amps.
    """
    available_ac_amps = (BREAKER_LIMIT_A - SAFETY_MARGIN_A) - (load_power_w / GRID_VOLTAGE_V)
    available_ac_watts_for_charging = max(available_ac_amps, 0) * GRID_VOLTAGE_V
    available_dc_watts = available_ac_watts_for_charging * CONVERSION_EFFICIENCY
    safe_dc_charge_current = available_dc_watts / BATTERY_VOLTAGE

    return round(max(safe_dc_charge_current, 0), 1)
from config_loader import config

BREAKER_LIMIT_A = config["breaker_safety"]["breaker_limit_a"]
SAFETY_MARGIN_A = config["breaker_safety"]["safety_margin_a"]
GRID_VOLTAGE_V = config["breaker_safety"]["grid_voltage_v"]
CONVERSION_EFFICIENCY = config["breaker_safety"]["conversion_efficiency"]

BATTERY_VOLTAGE = config["battery"]["nominal_voltage"]
BATTERY_MAX_CHARGE_A = config["battery"]["max_charge_current_a"]


def calculate_safe_charge_current(load_power_w, pv_power_w=0):
    """
    Returns the max safe DC charge current (A, on the battery side) given
    current house load AND current solar production - SNU mode charges
    from BOTH solar and EDL simultaneously, so their combined DC current
    into the battery must respect the battery's own disjoncteur rating
    (config: battery.max_charge_current_a), separately from the AC-side
    breaker constraint on EDL's contribution alone.
    """
    available_ac_amps = (BREAKER_LIMIT_A - SAFETY_MARGIN_A) - (load_power_w / GRID_VOLTAGE_V)
    available_ac_watts_for_charging = max(available_ac_amps, 0) * GRID_VOLTAGE_V
    available_dc_watts = available_ac_watts_for_charging * CONVERSION_EFFICIENCY
    ac_limited_current = available_dc_watts / BATTERY_VOLTAGE

    solar_charge_current_a = pv_power_w / BATTERY_VOLTAGE
    battery_disjoncteur_headroom = BATTERY_MAX_CHARGE_A - solar_charge_current_a

    safe_current = min(ac_limited_current, battery_disjoncteur_headroom)

    return round(max(safe_current, 0), 1)
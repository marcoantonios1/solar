from config_loader import config

BREAKER_LIMIT_A = 20
SAFETY_MARGIN_A = 2  # stay this far under the breaker's rated limit
GRID_VOLTAGE_V = 230


def calculate_safe_charge_current(load_power_w):
    """
    Returns the max safe AC charge current (A) given current house load,
    leaving room under the 20A smart breaker for both EDL charging and
    EDL house-load (relevant only when in UTI mode, where both draw from
    the same breaker simultaneously).
    """
    load_amps = load_power_w / GRID_VOLTAGE_V
    available_amps = (BREAKER_LIMIT_A - SAFETY_MARGIN_A) - load_amps
    return max(available_amps, 0)
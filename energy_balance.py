from config_loader import config

ROUND_TRIP_EFFICIENCY = config["battery"]["round_trip_efficiency"]


def calculate_energy_balance(solar_expected_kwh, battery_available_kwh, house_expected_kwh):
    """
    Core shared arithmetic for all three predictive layers:
    energy_balance = solar_expected + (battery_available * round_trip_efficiency) - house_expected

    Only the battery term is discounted - stored energy loses ~8-12% to
    inverter conversion when discharged (LFP + inverter round-trip loss).
    Solar and house terms aren't stored/converted the same way, so they
    stay as-is. Without this, balances were systematically optimistic on
    battery-heavy days.

    All inputs are energy over the SAME time period, in kWh (not instantaneous watts).
    A negative balance means a predicted EDL shortfall.

    Returns a dict with the raw balance and a simple classification.
    """
    effective_battery_kwh = battery_available_kwh * ROUND_TRIP_EFFICIENCY
    balance_kwh = solar_expected_kwh + effective_battery_kwh - house_expected_kwh

    if balance_kwh >= 0:
        classification = "surplus"
        shortfall_kwh = 0.0
    else:
        classification = "shortfall"
        shortfall_kwh = abs(balance_kwh)

    return {
        "balance_kwh": round(balance_kwh, 3),
        "classification": classification,
        "shortfall_kwh": round(shortfall_kwh, 3),
        "solar_expected_kwh": solar_expected_kwh,
        "battery_available_kwh": battery_available_kwh,
        "house_expected_kwh": house_expected_kwh,
    }
def calculate_energy_balance(solar_expected_kwh, battery_available_kwh, house_expected_kwh):
    """
    Core shared arithmetic for all three predictive layers:
    energy_balance = solar_expected + battery_available - house_expected

    All inputs are energy over the SAME time period, in kWh (not instantaneous watts).
    A negative balance means a predicted EDL shortfall.

    Returns a dict with the raw balance and a simple classification.
    """
    balance_kwh = solar_expected_kwh + battery_available_kwh - house_expected_kwh

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
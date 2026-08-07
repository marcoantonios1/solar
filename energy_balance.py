from config_loader import config

ROUND_TRIP_EFFICIENCY = config["battery"]["round_trip_efficiency"]


def calculate_energy_balance(solar_expected_kwh, battery_available_kwh, house_expected_kwh):
    """
    Core shared arithmetic for all three predictive layers - the ONE
    place this calculation happens (Issue #176-review bug 1: previously,
    output_mode_manager.py had its own separate, undiscounted copy of
    this formula, meaning Layer 2/relax never actually got the #136
    round-trip efficiency fix at all).

    balance_kwh = solar_expected + (battery_available * round_trip_efficiency) - house_expected
    Only the battery term is discounted (~8-12% round-trip loss through
    the battery/inverter) - used for SHORTFALL CLASSIFICATION.

    raw_ending_battery_kwh = solar_expected + battery_available - house_expected
    (undiscounted) - used ONLY for chaining to the next day's starting
    point (Issue #176-review bug 5: chaining the discounted balance_kwh
    forward caused the discount to compound day over day, ~0.92^7 by the
    end of a week - the efficiency loss should be applied once, fresh,
    each time stored energy is actually used, not stacked every time it's
    carried forward untouched).

    All inputs are energy over the SAME time period, in kWh.
    """
    effective_battery_kwh = battery_available_kwh * ROUND_TRIP_EFFICIENCY
    balance_kwh = solar_expected_kwh + effective_battery_kwh - house_expected_kwh
    raw_ending_battery_kwh = solar_expected_kwh + battery_available_kwh - house_expected_kwh

    if balance_kwh >= 0:
        classification = "surplus"
        shortfall_kwh = 0.0
    else:
        classification = "shortfall"
        shortfall_kwh = abs(balance_kwh)

    return {
        "balance_kwh": round(balance_kwh, 3),
        "raw_ending_battery_kwh": round(raw_ending_battery_kwh, 3),
        "classification": classification,
        "shortfall_kwh": round(shortfall_kwh, 3),
        "solar_expected_kwh": solar_expected_kwh,
        "battery_available_kwh": battery_available_kwh,
        "house_expected_kwh": house_expected_kwh,
    }
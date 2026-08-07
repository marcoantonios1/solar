import pandas as pd

from config_loader import config
from providers import solar_forecast, load_model
from pipeline import split_day_night_hours
from battery_model import get_battery_available_kwh
from energy_balance import calculate_energy_balance
from solar_model import get_sun_times_for_date
from providers import solar_forecast as _prefetch_solar_forecast

CAPACITY_KWH_USABLE = config["battery"]["capacity_kwh_usable"]
NUM_CYCLES = config["prediction"]["num_cycles"]


def get_current_soc(conn):
    """Returns the most recent logged battery_soc, or None if no readings exist."""
    row = conn.execute(
        "SELECT battery_soc FROM readings WHERE battery_soc IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def get_daily_predictions(conn):
    """
    Now built on the shared providers (Issue #150) for solar and house
    load, instead of separate implementations. Battery input stays
    special-cased on purpose: today's real pre-sunrise reading and the
    chained projection for future days are genuinely distinct data
    sources from the generic "latest live reading" battery_state()
    provider, so they're not forced through it.
    """
    today_str = pd.Timestamp.now(tz="Asia/Beirut").strftime("%Y-%m-%d")
    current_soc = get_current_soc(conn)

    # Need NUM_CYCLES+1 sunrises to define NUM_CYCLES sunrise-to-sunrise cycles
    dates = [(pd.Timestamp(today_str) + pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(NUM_CYCLES + 1)]
    sunrises = [get_sun_times_for_date(d)[0] for d in dates]

    # Prefetch enough forecast data upfront for the FURTHEST cycle, so all
    # 7 subsequent solar_forecast() calls hit the cache instead of each
    # triggering its own fetch (Issue #176-review bug 3 - the cache was
    # missing on nearly every call since days_needed grew each cycle)
    from providers import solar_forecast as _prefetch_solar_forecast
    _prefetch_solar_forecast(sunrises[0], sunrises[-1])

    predictions = []
    chained_battery_kwh = None

    for i in range(NUM_CYCLES):
        date_str = dates[i]
        cycle_start = sunrises[i]
        cycle_end = sunrises[i + 1]

        solar_result = solar_forecast(cycle_start, cycle_end)
        if solar_result.failed:
            if i == 0:
                # Today's own forecast failed - abort the WHOLE run rather
                # than silently letting a later day's data end up at
                # predictions[0], which callers assume is always today
                # (Issue #176-review bug 2)
                return None
            continue  # a FUTURE cycle failing is still safe to skip - today's slot is already secured

        day_hours, night_hours = split_day_night_hours(cycle_start, cycle_end)
        house_result = load_model(conn, day_hours, night_hours)

        if date_str == today_str:
            battery_info = get_battery_available_kwh(conn, date_str=today_str)
            battery_available_kwh = battery_info["battery_available_kwh"] or 0
            battery_source = battery_info["source"]
        elif chained_battery_kwh is not None:
            battery_available_kwh = chained_battery_kwh
            battery_source = "chained_from_previous_day"
        else:
            battery_available_kwh = 0
            battery_source = "conservative_zero_assumption"

        balance = calculate_energy_balance(
            solar_expected_kwh=solar_result.value_kwh,
            battery_available_kwh=battery_available_kwh,
            house_expected_kwh=house_result.value_kwh
        )

        chained_battery_kwh = max(0, min(balance["raw_ending_battery_kwh"], CAPACITY_KWH_USABLE))

        battery_recharge_status = None
        if date_str == today_str and current_soc is not None and balance["classification"] == "surplus":
            net_after_recharge = balance["balance_kwh"] - CAPACITY_KWH_USABLE
            battery_recharge_status = {
                "current_soc_pct": current_soc,
                "net_after_recharge_kwh": round(net_after_recharge, 2),
                "will_reach_full": balance["balance_kwh"] >= CAPACITY_KWH_USABLE,
            }

        predictions.append({
            "date": date_str,
            "solar_expected_kwh": solar_result.value_kwh,
            "house_expected_kwh": house_result.value_kwh,
            "battery_available_kwh": round(battery_available_kwh, 2),
            "battery_source": battery_source,
            "balance_kwh": balance["balance_kwh"],
            "classification": balance["classification"],
            "shortfall_kwh": balance["shortfall_kwh"],
            "battery_recharge_status": battery_recharge_status,
        })

    return predictions
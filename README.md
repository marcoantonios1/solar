# Growatt SPF 5000 ES — Raspberry Pi Modbus Connection

## Project Structure

```
edl_solar_automation/
├── config.json # All settings: thresholds, register map, panel/battery specs, location, tariff
├── config_loader.py # Loads config.json once, shared by all modules
├── inverter.py # Modbus connection, port auto-detection, register read/write, mode constants
├── db.py # SQLite schema + all read/write helpers (readings, mode_changes, edl_events)
├── rules.py # Rule 1 / Rule 2 decision logic
├── utils.py # Heartbeat file, manual override flag check
├── weather.py # Open-Meteo integration (cloud cover, GHI/DNI/DHI, ambient temp)
├── solar_model.py # pvlib-based expected power: clear-sky and weather-adjusted variants
├── main.py # Poll loop — ties everything together, entry point
├── tests/ # Connection tests, register exploration, pvlib prototypes
│ ├── init.py
│ ├── test_modbus.py
│ ├── test_pvlib.py
│ ├── test_irradiance.py
│ └── compare_expected_actual.py
├── reports/ # Standalone reporting scripts
│ ├── init.py
│ ├── report.py # EDL cost / usage summary report
│ └── performance_check.py # Panel expected-vs-actual performance check
└── inverter.db # SQLite database (created automatically on first run)
```

Run the automation with `python3 main.py`.
Run the EDL summary report anytime with `python3 -m reports.report [days]`.
Run the panel performance check anytime with `python3 -m reports.performance_check [hours]`.

## Verified Connection Settings

| Setting     | Value                          |
|-------------|---------------------------------|
| Connection  | USB (USB-A on Pi → USB-B on inverter) |
| Device path | `/dev/ttyUSB0` or `/dev/ttyUSB1` (auto-detected at startup — see `find_inverter_port()` in `inverter.py`) |
| Library     | `pymodbus` 3.14.0                |
| Baudrate    | 9600                             |
| Parity      | N                                 |
| Stopbits    | 1                                 |
| Bytesize    | 8                                 |
| Device ID / Slave ID | 1                        |

**Note on pymodbus 3.14 API:** this version renamed the `slave` keyword argument to `device_id`. Using `slave=` will raise `TypeError: got an unexpected keyword argument`. Use `device_id=` instead.

**Note on device path stability:** the USB-serial device has been observed to shift between `/dev/ttyUSB0` and `/dev/ttyUSB1` across disconnects/reconnects. `main.py` auto-detects the correct port at startup, and re-detects mid-run if all retries on the current connection fail.

## Install

```bash
pip install pymodbus pvlib requests --break-system-packages
```

## Confirmed Working Read (Input Registers)

The actual implementation lives in `inverter.py` (`read_values_once`). The core pattern:

```python
from pymodbus.client import ModbusSerialClient

client = ModbusSerialClient(
    port='/dev/ttyUSB0',
    baudrate=9600,
    parity='N',
    stopbits=1,
    bytesize=8,
    timeout=3
)

if client.connect():
    result = client.read_input_registers(0, count=20, device_id=1)
    if not result.isError():
        regs = result.registers
        print("Battery Voltage (V):", regs[17] / 100)
        print("Battery SOC (%):", regs[18])
    client.close()
```

Verified against LCD display on 2026-07-10: Battery Voltage 53.02V (LCD: 53.0V), Battery SOC 95% (LCD/BMS: 95%). Match confirmed.

## Input Register Map (0-indexed, from community reverse-engineering — [sdsolomo/growatt-x000ES](https://github.com/sdsolomo/growatt-x000ES))

| Address | Field                       | Scale  | Notes |
|---------|------------------------------|--------|-------|
| 0       | StatusCode                   | 1      | 0=Standby, 2=Discharge, 5=PV Charge, 6=AC Charge, 12=PV charge and discharge |
| 1       | PV1 Voltage (V)               | ÷10    | |
| 2       | PV2 Voltage (V)               | ÷10    | |
| 3-4     | PV1 Charge Power (W)          | ÷10    | High/Low word pair |
| 5-6     | PV2 Charge Power (W)          | ÷10    | High/Low word pair |
| 7       | Buck1 Current (A)             | ÷10    | |
| 8       | Buck2 Current (A)             | ÷10    | |
| 9-10    | Output Active Power (W)       | ÷10    | High/Low word pair |
| 11-12   | Output Apparent Power (VA)    | ÷10    | High/Low word pair |
| 13-14   | AC Charge Watt (W)            | ÷10    | High/Low word pair |
| 15-16   | AC Charge VA                  | ÷10    | High/Low word pair |
| 17      | **Battery Voltage (V)**       | ÷100   | Confirmed accurate |
| 18      | **Battery SOC (%)**           | ÷1     | Confirmed accurate |
| 19      | Bus Voltage (V)                | ÷10    | |
| 20      | Grid/AC Input Voltage (V)     | ÷10    | |
| 21      | Line Frequency (Hz)           | ÷100   | |
| 22      | Output Voltage (V)            | ÷10    | |
| 23      | Output Frequency (Hz)         | ÷100   | |
| 24      | Output DC Voltage (V)         | ÷10    | |
| 25      | Inverter Temp (°C)             | ÷10    | |
| 26      | DCDC Temp (°C)                 | ÷10    | |
| 27      | Load Percent (%)              | ÷10    | |
| 34      | Output Current (A)            | ÷10    | |
| 35      | Inverter Current (A)          | ÷10    | |
| 36-37   | AC Input Watt (W)             | ÷10    | High/Low word pair |
| 38-39   | AC Input VA                   | ÷10    | High/Low word pair |
| 40      | Fault Bit                      | 1      | |
| 41      | Warn Bit                       | 1      | |
| 68      | AC Charge Battery Current (A) | ÷10    | |
| 77-78   | Battery Watt (signed, W)      | ÷10    | Positive = discharge, negative = charge |

## Holding Register Map (Writable Settings)

Source: official Growatt OffGrid SPF5000 Modbus RS485 RTU Protocol V0.11 + empirical testing on this unit

| Reg | Name | Values | Notes |
|-----|------|--------|-------|
| 00 | On/Off | 0x0000=Standby off/Output enable, 0x0001=Standby on/Output enable | |
| **01** | **OutputConfig** | **0=BAT First, 1=PV First, 2=UTI First** | ✅ Confirmed: register read `2` matched LCD showing "UTI". SBU/SOL/SUB value mapping not yet individually tested — only UTI validated so far. |
| **02** | **ChargeConfig** | **0=PV first (CSO), 1=PV&UTI (SNU), 2=PV Only (OSO)** | ✅ Confirmed: 0=CSO, 1=SNU, 2=OSO. This is LCD Program 14. Automation default is OSO — EDL never charges unless Rule 1 explicitly allows it (see `rules.py`). |
| 03 | UtiOutStart | 0-23 (hour) | |
| 04 | UtiOutEnd | 0-23 (hour) | |
| 05 | UtiChargeStart | 0-23 (hour) | |
| 06 | UtiChargeEnd | 0-23 (hour) | |
| 34 | MaxChargeCurr | 10-130 (×1A) | Max charge current setting — will be dynamically adjusted in Phase 4's breaker-aware logic |
| 35 | BulkChargeVolt | 500-580 (×0.1V) | |
| 36 | FloatChargeVolt | 500-560 (×0.1V) | |
| **37** | **BatLowToUtiVolt*** | **SOC % × 10 (e.g. 500 = 50%)** | ✅ Confirmed: this is LCD **Program 12** (Battery-to-Grid SOC Gate). Doc labels it a voltage register (444-514, ×0.1V) for lead-acid systems, but on this unit (Battery Type = Lithium w/ BMS) it's repurposed to store SOC percentage instead. Verified by changing LCD 40%→50%, register moved 400→500. **Note: AC charging is gated by this register regardless of CSO/SNU/OSO — SOC must be below this threshold for EDL to charge at all.** |
| 39 | Battery Type | 0=Lead_Acid, 1=Lithium, 2=CustomLead_Acid | |
| 45-50 | Sys Year/Month/Day/Hour/Min/Sec | | System clock — confirmed via live diff test, NOT Program 12 |
| 76-77 | Rate Watt (H/L) | ×0.1W | Rated active power |

*Register name kept from the official doc for traceability, despite the repurposed meaning on this firmware/battery-type combination.

## Confirmed Input Registers — Load & AC Input Power

| Reg | Name | Scale | Use |
|-----|------|-------|-----|
| 9-10 | Output Active Power (H/L) | ÷10 (W) | ✅ `load_power` — confirmed against Load Percent (reg 27) |
| 27 | Load Percent | ÷10 (%) | Cross-check reference for load_power |
| 13-14 | AC Charge Watt (H/L) | ÷10 (W) | ✅ `ac_charge_power` — power specifically flowing from EDL into the battery. Used for `total_kwh_charged_during` calculation. |
| 36-37 | AC Input Watt (H/L) | ÷10 (W) | Total power currently being drawn from EDL (charging + any house load) |
| 20 | Grid Volt (AC input voltage) | ÷10 (V) | ✅ `edl_present` = (value > ~100) — confirmed reads 0 when EDL off |

## Manual Override

To pause automatic mode-switching (e.g., during EV charging, manual testing, or troubleshooting):

    touch MANUAL_MODE

The script will continue polling and logging readings, but will not write any charger-mode changes to the inverter while this file exists. Safe to change settings manually on the LCD during this time.

To resume automation:

    rm MANUAL_MODE

Takes effect on the next poll cycle (no restart needed).

## EDL Event Tracking (Phase 2)

### edl_events Table Schema

| Column | Type | Description |
|--------|------|--------------|
| event_id | INTEGER (PK) | Unique identifier |
| start_time | TEXT | When EDL turned on |
| end_time | TEXT | When EDL turned off (NULL while session is open) |
| duration_min | REAL | Session length in minutes |
| avg_pv_power_during | REAL | Average solar power (W) during the session — context only, not used in cost calc |
| total_kwh_charged_during | REAL | Energy delivered to the battery via EDL during the session, calculated from `ac_charge_power` (avg power × duration) |
| reason | TEXT | Why EDL was allowed — pulled from the matching `mode_changes` trigger_reason if one exists within a 10-minute lookback window; falls back to a restart note or "no matching mode change" message otherwise |
| cost_usd | REAL | Dollar cost of this event's kWh, calculated using the tiered rate |

### How EDL Sessions Are Detected

Every poll cycle compares the current `edl_present` reading (from AC input voltage, register 20) against the previous cycle's value:
- **False → True**: opens a new `edl_events` row with `start_time = now`
- **True → False**: closes the open row, calculating `duration_min`, `avg_pv_power_during`, `total_kwh_charged_during`, `reason`, and `cost_usd`

**Restart handling:** if the script restarts while EDL was already on, it resumes tracking the existing open row rather than creating a duplicate. If EDL was on before a restart but off by the time the script resumes, the stale row is closed with an approximate end time and a note flagging that the exact off-time is unknown.

### Cost Calculation Methodology

Cost uses the tiered EDL rate from `config.json` (`edl_tariff`):
- Tier 1: `$0.10/kWh` for the first `100 kWh` used in the calendar month
- Tier 2: `$0.27/kWh` for anything beyond that

For each event, the script sums `total_kwh_charged_during` from all *other* events already recorded earlier in the same calendar month, to determine how much tier-1 allowance remains. The current event's kWh is then split across tier 1 (remaining allowance) and tier 2 (the rest, if any), and costed accordingly.

**Known limitation:** EV charging bypasses the inverter entirely (drawn directly from the main breaker), so it's invisible to this system and not included in any kWh or cost totals. See `ev_charging` note in `config.json`.

### Weekly/Monthly Summary Report

```bash
python3 -m reports.report        # last 7 days (default)
python3 -m reports.report 30     # last 30 days
python3 -m reports.report 1      # last 24 hours
```

Reports include:
- Total EDL sessions and total duration
- Total kWh delivered and total $ cost
- Breakdown by trigger reason (Rule 1 / Program 12 cutoff / manual / other)
- A comparison estimate: what EDL would have cost under the *old* always-on behavior (assuming EDL supplied 100% of house load, no solar/battery contribution) vs. the actual automated cost. This is a simplified worst-case baseline for context, not a measurement of real historical always-on usage.

## Weather Integration & Panel Performance Monitoring (Phase 3)

### Weather Data (`weather.py`)

Pulls current cloud cover, real solar irradiance (GHI, DNI, DHI), and ambient temperature from Open-Meteo (free, no API key required). Fetched at most once every `polling.weather_fetch_interval_seconds` (default 900s / 15 min) and cached between poll cycles — the main loop reuses the cached value rather than calling the API every cycle.

### Two Expected-Power Models (`solar_model.py`)

Both use the same pvlib plane-of-array projection (panel tilt/azimuth, horizon obstruction, temperature derating, age degradation) but differ in their irradiance source:

**1. Clear-sky model** (`get_expected_power`) — theoretical maximum output from sun position alone, using pvlib's Ineichen clear-sky model. **Only reliable on genuinely clear days.** On any day with real cloud cover, the gap between this and actual output swings wildly and meaninglessly (observed -3% to -67% within a single minute at ~45% cloud cover) — not usable as a general health check.

**2. Weather-adjusted model** (`get_weather_adjusted_expected_power`) — projects Open-Meteo's real, already cloud-adjusted GHI/DNI/DHI onto the panel plane instead of a hypothetical clear sky. **Validated against real hardware at 57-58% cloud cover: 94-95% match with actual output** (e.g. predicted 2466.7W vs. actual 2354.0W). This is the model used for ongoing health monitoring and will be the one Phase 4's predictive layers rely on.

Both models apply:
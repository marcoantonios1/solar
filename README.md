# Growatt SPF 5000 ES — Raspberry Pi Modbus Connection

## Project Structure

```
edl_solar_automation/
├── config.json # All settings: thresholds, register map, panel/battery specs, location, tariff
├── config_loader.py # Loads config.json once, shared by all modules
├── inverter.py # Modbus connection, port auto-detection, register read/write, mode constants
├── db.py # SQLite schema + all read/write helpers
├── rules.py # Rule 1 (Layer 3 live safety net)
├── utils.py # Heartbeat file, manual override flag check
├── weather.py # Open-Meteo integration: current + hourly forecast (GHI/DNI/DHI, temp)
├── solar_model.py # pvlib-based expected power: clear-sky and weather-adjusted variants, sun times
├── load_model.py # Household load model: real historical + seasonal fallback, day/night split
├── battery_model.py # Pre-sunrise battery buffer calculation
├── energy_balance.py # Core shared formula: solar + battery - house = balance
├── daily_forecast.py # 7-day sunrise-to-sunrise solar forecast (Layer 1 input)
├── daily_predictor.py # Combines forecast + load + battery into daily predictions (Layer 1)
├── output_mode_manager.py # Layer 1 decision logic: three-tier mode selection
├── near_term_check.py # Layer 2: daytime "will battery reach full" projection
├── near_term_decision.py # Layer 2: escalate-only correction logic
├── breaker_safety.py # AC breaker -> safe DC charge current calculation
├── charge_throttle.py # Applies breaker-safe charge current when in SNU+UTI
├── main.py # Poll loop — Layer 3 + readings + EDL tracking, entry point
├── run_daily_prediction.py # Standalone script for the daily (Layer 1) systemd timer
├── run_near_term_check.py # Standalone script for the hourly (Layer 2) systemd timer
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

All terms in kWh over the same time period. Negative balance = predicted shortfall. (`energy_balance.py`)

### Layer 1 — Daily Predictive (`daily_predictor.py`, `output_mode_manager.py`, `run_daily_prediction.py`)

Runs once daily via systemd timer (08:00). For each of the next 7 sunrise-to-sunrise cycles (not calendar days — see below):

- **Solar expected**: `daily_forecast.py` sums the weather-adjusted model across each cycle's hourly forecast
- **House expected**: `load_model.py`'s day/night rates × that cycle's actual day/night hour lengths
- **Battery available**: today only — the real pre-sunrise SOC low (`battery_model.py`); future days conservatively assume 0 (unknowable in advance)

**Sunrise-to-sunrise cycles, not midnight-to-midnight:** solar/house accounting is bucketed by the most recent sunrise, so a "day" runs from today's sunrise to tomorrow's sunrise — matching exactly when `battery_available` is measured and covering one full day+night period without double- or half-counting either boundary.

**Three-tier decision** (`output_mode_manager.py`):
- Comfortable surplus → **OSO + SBU** (default, minimize EDL)
- Small deficit → **OSO + UTI** (EDL covers house load directly, spares battery, no charging spend)
- Larger deficit, or today's battery projected to fall short of reaching full → **SNU + UTI** (EDL charges fully and powers house)

**Battery recharge check:** a day can show "surplus" on the basic balance calculation while still not being enough to actually recharge the battery to full from its current SOC — these are different questions. `daily_predictor.py` checks both and flags a near-miss even when the basic classification says "surplus."

### Layer 2 — Near-Term Correction (`near_term_check.py`, `near_term_decision.py`, `run_near_term_check.py`)

Runs hourly via systemd timer, **daytime only** — returns `None` (no action) outside sunrise-sunset. Projects, using **live current SOC** (not this morning's reading) and the remaining hours until tonight's sunset, whether the battery will actually reach full. If the live projection now shows risk that this morning's forecast didn't predict, escalates to SNU+UTI.

**Escalate-only, by design:** this layer never relaxes back toward OSO+SBU on its own — only Layer 1's next daily run does that. This is deliberate: EDL's real-world availability is unpredictable (rationing/scheduling), so once the system has proactively opened the door to EDL for the day, it stays open through the night rather than risking closing it right before an unpredictable EDL window appears. Whatever mode is active at sunset carries through unchanged until tomorrow's 8 AM run.

### Layer 3 — Live Safety Net (`rules.py`, evaluated every poll cycle in `main.py`)

Simplified to a pure, fast, forecast-independent check:
```python
if battery_soc < CRITICAL_SOC_FLOOR and edl_present:
    return SNU, UTI, "Rule 1: critical SOC floor + EDL present"
return OSO, None, "Default: no rule triggered"
```
No solar condition — this layer doesn't care what the sun is doing, only real-time SOC. **Independently verified**: fires and overrides a forced OSO+SBU state on its own, regardless of what Layer 1/2 had decided. Also confirmed to correctly leave output priority untouched (not revert to SBU) when it isn't firing — only actively pushes toward UTI when it fires, mirroring Layer 2's escalate-only philosophy.

### Household Load Model (`load_model.py`)

Splits load into day/night using **real sunrise/sunset per date** (via pvlib), not a fixed hour boundary — accurate across all seasons, not just whichever season the boundary was tuned for.

**Cold-start fallback:** `config.json` → `seasonal_load_estimate` provides rough per-season night-load estimates (grounded in real overnight battery-depletion math for summer; rough placeholders for other seasons) until at least 7 days of real historical data exist for that season/period, at which point real data takes over automatically. Daytime load, absent real data, is estimated as a fraction of the night estimate (currently 40%) — a rough placeholder, low-stakes since daytime load matters less for EDL decisions (solar is directly available then).

### Breaker-Aware Dynamic Charge Current (`breaker_safety.py`, `charge_throttle.py`)

When in SNU+UTI, EDL charging and EDL house-load draw from the same 20A smart breaker simultaneously. `calculate_safe_charge_current()` converts available AC breaker headroom into a safe **DC-side** charge current (Program 11 / Reg 38), correctly crossing the AC (230V) ↔ DC (~51V battery) voltage domains through power (watts), including a conversion-efficiency estimate (0.93) rather than comparing amps directly across domains.

`main.py` polls at a **fast interval (5s, configurable)** whenever currently in SNU+UTI, and normal interval otherwise — so load surges get caught and throttled quickly rather than waiting up to a full normal poll cycle.

**Safety margin:** `config.json` → `breaker_safety.safety_margin_a` (currently 4A, ~80% breaker utilization) leaves headroom for the register's write-settling delay and AC inrush current (motor/compressor startup can briefly draw several times normal running current — faster than any polling interval can react to). Started at 2A margin; increased to 4A after a real breaker trip during testing.

## Running as Services

Three systemd units:

| Service | Type | Purpose | Schedule |
|---------|------|---------|----------|
| `edl-solar.service` | continuous | Main loop: Layer 3, readings, EDL tracking | Always running |
| `edl-daily-prediction.service` + `.timer` | oneshot | Layer 1 | Daily @ 08:00 |
| `edl-near-term-check.service` + `.timer` | oneshot | Layer 2 | Hourly (self-limits to daylight) |

The two timer-triggered services run as **root** and use `ExecStartPre`/`ExecStopPost` to stop/restart `edl-solar.service` around their run (avoiding a Modbus port conflict — only one process can hold the serial port at a time). `ExecStopPost` (not `ExecStartPost`) guarantees `edl-solar.service` restarts even if the triggered script crashes.

**edl-solar.service** — `/etc/systemd/system/edl-solar.service`:
```ini
[Unit]
Description=EDL Solar Charging Automation
After=multi-user.target

[Service]
Type=simple
User=marco
WorkingDirectory=/home/marco/Documents/edl_solar_automation
ExecStart=/usr/bin/python3 -u /home/marco/Documents/edl_solar_automation/main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```
The `-u` flag forces unbuffered output — without it, logs won't appear in `journalctl` until the buffer fills.

**edl-daily-prediction.service** — `/etc/systemd/system/edl-daily-prediction.service`:
```ini
[Unit]
Description=EDL Solar Daily Prediction & Mode Adjustment
After=multi-user.target

[Service]
Type=oneshot
WorkingDirectory=/home/marco/Documents/edl_solar_automation
ExecStartPre=/usr/bin/systemctl stop edl-solar.service
ExecStart=/usr/bin/python3 -u /home/marco/Documents/edl_solar_automation/run_daily_prediction.py
ExecStopPost=/usr/bin/systemctl start edl-solar.service
StandardOutput=journal
StandardError=journal
```
`/etc/systemd/system/edl-daily-prediction.timer`:
```ini
[Unit]
Description=Run EDL daily prediction once per day

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

**edl-near-term-check.service** — same pattern, `run_near_term_check.py`, `OnCalendar=hourly` in its timer.

**Common commands:**
```bash
sudo systemctl daemon-reload                          # after editing any service/timer file
sudo systemctl restart edl-solar.service               # after editing any .py file
sudo systemctl start edl-daily-prediction.service       # manually trigger a run now (testing)
sudo systemctl start edl-near-term-check.service
systemctl list-timers 'edl-*'                            # see all schedules at once
journalctl -u edl-solar.service -f                        # live tail
journalctl -u edl-daily-prediction.service --since "5 minutes ago"
```

## Notable Bugs Found & Fixed

- **Wrong system timezone** (America/Adak instead of Asia/Beirut) caused every timestamp to be off by ~12 hours, silently corrupting time-based logic and weather correlation. Fixed via `sudo timedatectl set-timezone Asia/Beirut`.
- **CSO/SNU/OSO register values initially backwards** in code — fixed and verified against real hardware writes.
- **Charge current register: official doc's Reg 34 was wrong** for this unit's actual LCD "Program 11" — real register is **Reg 38** (Reg 93 mirrors the same value but 38 is authoritative), discovered via diff-test after a failed initial assumption.
- **Register write settling delay (~2s)** — an immediate read-back after certain writes (confirmed on Reg 38) can show the stale pre-write value; a short delay before reading resolves it.
- **Breaker trip during dynamic charge current testing** — initial safety margin (2A, ~90% breaker utilization) was insufficient against real AC inrush current and the register settling delay; increased to 4A (~80% utilization).
- **Output priority can be left stuck on UTI after manual testing** — since only Layer 1's daily run and Rule 1's active firing ever set output priority, and nothing currently de-escalates it otherwise, a manually-set UTI state (e.g. during testing) persists until the next Layer 1 run. Known limitation, not yet auto-corrected.

## Status

Phases 0-4 (MVP, Event Tracking, Weather/Panel Monitoring, Three-Layer Predictive Charging) complete and verified against real hardware, running live via systemd. Phase 5 (Visualization, Alerts, Monthly PDF Report) not yet started.
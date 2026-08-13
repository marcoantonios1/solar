# Growatt SPF 5000 ES — Raspberry Pi Modbus Connection

## Project Structure

```
edl_solar_automation/
├── config.json              # All settings: thresholds, register map, panel/battery specs, location, tariff
├── config_loader.py         # Loads config.json once, shared by all modules
├── inverter.py               # Modbus connection, port auto-detection, register read/write, mode constants
├── db.py                      # SQLite schema + all read/write helpers
├── rules.py                    # Layer 3 (Rule 1) - pure, fast, forecast-independent live safety net
├── arbiter.py                   # THE single decision point - arbitrates all layer proposals
├── proposal.py                   # Proposal dataclass shared by every layer
├── actuator.py                    # THE single write path - settling delay, verified read-back, retry, WriteGuard
├── pipeline.py                     # run_pipeline() - shared provider→balance→policy flow
├── providers.py                     # battery_state, load_model, solar_forecast - shared data providers
├── energy_balance.py                 # Core shared balance formula (round-trip efficiency included)
├── daily_predictor.py                 # Layer 1: 7-day chained forecast (in-loop, no longer a separate timer)
├── output_mode_manager.py             # classify_energy_balance() + decide_target_state() (Layer 1's proposal logic)
├── near_term_check.py                  # get_battery_projection() - Layer 2's proposal logic (in-loop)
├── near_term_decision.py               # get_tier_rank() - shared tier ranking used by the arbiter
├── charge_throttle.py                   # Breaker-safe charge current; relax_if_battery_full(); relax_rule1_early_if_recovered()
├── breaker_safety.py                     # AC breaker -> safe DC charge current calculation
├── battery_model.py                       # Pre-sunrise battery buffer calculation
├── load_model.py                           # Household load model, day/night split
├── solar_model.py                           # pvlib-based expected power, sun times
├── weather.py                                # Open-Meteo integration
├── utils.py                                   # Heartbeat file (tmpfs), MANUAL_MODE flag check
├── alerts.py                                   # Email alerting with per-type cooldown
├── check_heartbeat.py                            # Standalone heartbeat staleness check (own timer)
├── backup_database.py                             # Monthly SQLite backup to SD card (own timer)
├── main.py                                         # THE entry point - single continuous loop, all layers in-loop
├── tests/
│   └── test_regression_bugs.py                      # Regression suite seeded with real found bugs
├── reports/                                          # Standalone reporting scripts (report.py, monthly_*.py)
├── .env                                               # Email credentials (Gmail App Password) - gitignored
└── inverter.db → /mnt/edl-data/inverter.db (symlink)   # SQLite database, on a dedicated USB drive, WAL mode
```

All terms in kWh over the same time period. Negative balance = predicted shortfall. (`energy_balance.py`)

## Install

```bash
pip install pymodbus pvlib requests --break-system-packages
```

Note: the two systemd-timer-triggered scripts (`run_daily_prediction.py`, `run_near_term_check.py`) run as root, so these packages need installing for root too:
```bash
sudo python3 -m pip install pymodbus pvlib requests --break-system-packages
```

For the monthly report's email delivery, copy `.env.example` to `.env` and fill in a real Gmail App Password (requires 2-Step Verification enabled on the sending account — generate one at https://myaccount.google.com/apppasswords). Never commit `.env`.

### Unified Decision Architecture (`output_mode_manager.py`, `near_term_check.py`, `near_term_decision.py`, `charge_throttle.py`)

Layer 1 (daily) and Layer 2 (hourly) no longer run separate decision algorithms. Both call the same core function, `classify_energy_balance(solar_expected_kwh, battery_available_kwh, house_expected_kwh)`, which applies the three-tier thresholds (`SHORTFALL_THRESHOLD_KWH` / `CHARGE_NEEDED_THRESHOLD_KWH`) and returns one of:

- **Comfortable surplus → OSO + SBU** (default, minimize EDL)
- **Small deficit → OSO + UTI** (EDL covers house load directly, spares battery)
- **Larger deficit → SNU + UTI** (EDL charges fully and powers house)

This eliminates a real class of bugs found during live testing, where Layer 1 and Layer 2 used to disagree about what "escalating" even meant (e.g. Layer 2 once recommended full escalation for a projected 24-minute gain in overnight buffer, before this fix — see "Notable Bugs" below).

**What differs between layers is only the freshness of the inputs, not the logic:**

- **Layer 1** (`decide_target_state()` in `output_mode_manager.py`, runs once daily at 07:00): calls `classify_energy_balance()` with this morning's pre-sunrise SOC and the 7-day chained forecast, then wraps it with two Layer-1-specific escalation checks on top — the battery-recharge shortfall check, and tomorrow's shortfall lookahead (`TOMORROW_SHORTFALL_LOOKAHEAD_KWH`). These checks can only push the decision *up* a tier, never down.
- **Layer 2** (`get_battery_projection()` in `near_term_check.py`, runs hourly, daytime only): calls the same `classify_energy_balance()`, but with live current SOC and a short-range (today-only) forecast instead of this morning's numbers.

**Escalate-only enforcement (`near_term_decision.py`):** the shared function itself has no concept of "only escalate" - that's enforced explicitly in `apply_near_term_correction()`, which ranks the three tiers (`OSO+SBU` < `OSO+UTI` < `SNU+UTI` via `get_tier_rank()`) and only ever applies a fresh tier if it's *strictly higher* than whatever's currently active. If live conditions genuinely improve mid-day, Layer 2 correctly does nothing — only Layer 1's next daily run can relax the system back down. This is deliberate: EDL's real-world availability is unpredictable, so once the system has proactively opened the door to EDL for the day, it stays open rather than risking closing it right before an unpredictable EDL window appears.

**`relax_if_battery_full()` (`charge_throttle.py`) also ties into the same shared calculation** — but it's the one deliberate exception to escalate-only, since a genuinely full battery (a live, measured fact, not a forecast) removes any real benefit from staying escalated. It uses `get_live_projection_until_sunrise()` — a day/night-aware variant of the Layer 2 calculation that works at any time (unlike Layer 2, which has no role after sunset) — and relaxes down to whatever tier the live numbers actually justify, rather than a flat SOC threshold. It still preserves the "buffer for a predicted cloudy tomorrow" check on top (via `daily_predictions.decision_label`), so a proactively-built buffer isn't accidentally drained via SBU once the battery tops up.

### Layer 3 — Live Safety Net (`rules.py`, evaluated every poll cycle in `main.py`)

Simplified to a pure, fast, forecast-independent check:
```python
if battery_soc < CRITICAL_SOC_FLOOR and edl_present:
    return SNU, UTI, "Rule 1: critical SOC floor + EDL present"
return None, None, "Default: no rule triggered"
```
No solar condition — this layer doesn't care what the sun is doing, only real-time SOC. **Independently verified**: fires and overrides a forced OSO+SBU state on its own, regardless of what Layer 1/2 had decided. Also confirmed to correctly leave output priority untouched (not revert to SBU) when it isn't firing — only actively pushes toward UTI when it fires, mirroring Layer 2's escalate-only philosophy.

### Household Load Model (`load_model.py`)

Splits load into day/night using **real sunrise/sunset per date** (via pvlib), not a fixed hour boundary — accurate across all seasons, not just whichever season the boundary was tuned for.

**Cold-start fallback:** `config.json` → `seasonal_load_estimate` provides rough per-season night-load estimates (grounded in real overnight battery-depletion math for summer; rough placeholders for other seasons) until at least 7 days of real historical data exist for that season/period, at which point real data takes over automatically. Daytime load, absent real data, is estimated as a fraction of the night estimate (currently 40%) — a rough placeholder, low-stakes since daytime load matters less for EDL decisions (solar is directly available then).

### Breaker-Aware Dynamic Charge Current (`breaker_safety.py`, `charge_throttle.py`)

When in SNU+UTI, EDL charging and EDL house-load draw from the same 20A smart breaker simultaneously. `calculate_safe_charge_current()` converts available AC breaker headroom into a safe **DC-side** charge current (Program 11 / Reg 38), correctly crossing the AC (230V) ↔ DC (~51V battery) voltage domains through power (watts), including a conversion-efficiency estimate (0.93) rather than comparing amps directly across domains.

`main.py` polls at a **fast interval (5s, configurable)** whenever currently in SNU+UTI, and normal interval otherwise — so load surges get caught and throttled quickly rather than waiting up to a full normal poll cycle.

**Safety margin:** `config.json` → `breaker_safety.safety_margin_a` (currently 4A, ~80% breaker utilization) leaves headroom for the register's write-settling delay and AC inrush current (motor/compressor startup can briefly draw several times normal running current — faster than any polling interval can react to). Started at 2A margin; increased to 4A after a real breaker trip during testing.

## Panel Performance Derate Factor (`config.json` -> `panels.temporary_performance_derate`)

Real, sustained panel underperformance was found 2026-08-12 (~20-23% below
the weather-adjusted model, consistent across a full day - the documented
signature of a real physical loss like dirty panels, not weather noise).
Every layer's decision depends on this "expected solar" figure, so rather
than let every calculation stay wrong, a temporary derate factor is applied
uniformly in `solar_model.py` until the panels are physically cleaned.

**This is NOT automatically maintained - it requires deliberate, periodic
human review, by design.** A fully automatic recalibration risks silently
applying a wrong number from a temporary anomaly (a dust storm, brief
shading, a bad batch of readings) with no one noticing until real
consequences show up. Given this number directly affects real EDL spending
decisions, a human review step is a deliberate safety choice.

### Recalibrating

```bash
python3 recalibrate_panel_performance.py           # report only - shows current vs recommended
python3 recalibrate_panel_performance.py --apply    # also writes the new value to config.json
```

After running with `--apply`, two manual steps are still required:

1. **Add a new entry to `DERATE_CHANGE_HISTORY`** at the top of
   `recalibrate_panel_performance.py`, with today's date and the new
   factor - this is what lets FUTURE recalibrations correctly normalize
   historical readings across every past change, not just the most recent
   one. Forgetting this step causes incorrect normalization (a real bug
   caught live 2026-08-13, now covered by a regression test).
2. **Restart `edl-solar.service`** so the live system picks up the new value:
   `sudo systemctl restart edl-solar.service`

**When to run this:** periodically (e.g., monthly) to keep the derate
honest as real conditions change, and especially right after physically
cleaning the panels (at which point the derate should trend back toward
1.0, and may eventually be removed from `config.json` entirely once
performance is confirmed restored).

## Monthly PDF Report (Phase 5)

### Data Layer (`reports/monthly_data.py`)

Aggregates existing tables (`readings`, `edl_events`, `mode_changes`, `daily_predictions`, `system_errors`, `manual_mode_log`) into six report sections — no new tracking required beyond what Phases 0-4 already log:

- **Executive summary** — total solar, total EDL cost, savings vs. old always-on estimate, longest EDL session
- **Monthly totals** — house/solar kWh, EDL sessions (charged vs. present-but-blocked), EDL kWh/cost
- **Daily breakdown** — per-day solar/house/EDL figures plus each EDL session's start/end time
- **Solar performance** — average expected-vs-actual gap (weather-adjusted model), a *separate* clear-sky-only gap figure, average cloud cover and ambient temperature, sustained-underperformance episode count (reuses `performance_check.py`'s bucketed/sustained methodology, not raw reading counts)
- **Battery health** — lowest SOC, hours spent near the critical floor, rough cycle-count estimate (derived from load/solar/EDL-charge readings, not direct current measurement)
- **System health** — Modbus read failures and reconnects, unhandled crashes (see below), MANUAL_MODE hours, longest gap in logging (flags whether the month's totals are fully trustworthy)

Accepts either a rolling `days=N` window (for testing) or explicit `start_str`/`end_str` calendar boundaries (for a real month).

### Charts (`reports/monthly_charts.py`)

Six charts, all sourced from the same daily-breakdown data:
1. Daily EDL kWh (bar)
2. Daily solar output (line)
3. Daily solar: expected vs. actual (overlay line)
4. Daily minimum battery SOC, with critical-floor reference line — missing-data days show a gap, not a misleading 0%
5. Daily house load (line)
6. EDL usage by trigger reason (horizontal bar, reuses `report.py`'s `categorize_reason()`)

### PDF Assembly (`reports/monthly_pdf.py`)

Built with `reportlab`. Section order: Executive Summary → Monthly Totals → Daily Breakdown → Solar Performance → Battery Health → System Health → Charts. Table cells use `Paragraph` (not plain strings) so long content — many EDL sessions in one day, long header labels — wraps within its column instead of clipping or overflowing into adjacent cells.

Deliberately excluded: raw per-reading dumps, predictive/forecast content (belongs in a forward-looking view, not a retrospective), any LLM-generated narrative text.

### Email Delivery (`reports/send_monthly_report.py`)

Sends the generated PDF as an email attachment via Gmail SMTP (App Password required — see Install section). Computes the *previous full calendar month's* boundaries (1st through last day), not a rolling 30-day window, so the report always covers exactly one real month.

Run manually anytime:
```bash
python3 -m reports.send_monthly_report
```
Generated PDFs are archived in `reports_archive/report_YYYY-MM.pdf`.

**Crash safety:** all three write-capable entry points (`main.py`'s loop, `run_daily_prediction.py`, `run_near_term_check.py`) wrap their core logic in a try/except that logs unexpected exceptions with a full traceback to `system_errors` (category `crash`) rather than silently dying or losing the diagnostic trail. `finally: client.close()` ensures the Modbus connection always releases cleanly.

## Running as a Service

**One continuous service handles everything** - readings, EDL tracking, and all three decision layers (Rule 1, Layer 1, Layer 2) run in a single process, arbitrated by `arbiter.py` and applied through `actuator.py`, the sole write path. There are no more separate Layer 1/Layer 2 timers or root-run scripts - that architecture was consolidated and the old timer-triggered scripts (`run_daily_prediction.py`, `run_near_term_check.py`) have been removed.

| Service | Purpose | Schedule |
|---------|---------|----------|
| `edl-solar.service` | Everything: readings, Rule 1, Layer 1, Layer 2, relax, arbitration | Always running |
| `edl-heartbeat-check.service` + `.timer` | Alerts if the heartbeat goes stale | Every 15 min |
| `edl-db-backup.service` + `.timer` | SQLite API backup to SD card | 1st of month @ 03:00 |
| `edl-monthly-report.service` + `.timer` | Monthly PDF report + email | 1st of month @ 09:00 |

**edl-solar.service** — `/etc/systemd/system/edl-solar.service`:
```ini
[Unit]
Description=EDL Solar Charging Automation
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=marco
WorkingDirectory=/home/marco/Documents/edl_solar_automation
ExecStart=/usr/bin/python3 -u /home/marco/Documents/edl_solar_automation/main.py
RuntimeDirectory=edl-solar
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```
`RuntimeDirectory=edl-solar` creates `/run/edl-solar/` (tmpfs) automatically for the heartbeat file - no SD card wear from the highest-frequency write in the whole system. `Restart=always` (not `on-failure`) - exit codes are honest now, so a clean exit should still restart.

**Common commands:**
```bash
sudo systemctl daemon-reload                  # after editing the service file
sudo systemctl restart edl-solar.service       # after editing any .py file
systemctl list-timers 'edl-*'                   # see all schedules
journalctl -u edl-solar.service -f               # live tail
python3 -m pytest tests/test_regression_bugs.py -v   # run before any deploy
```


## Notable Bugs Found & Fixed

- **Wrong system timezone** (America/Adak instead of Asia/Beirut) caused every timestamp to be off by ~12 hours, silently corrupting time-based logic and weather correlation. Fixed via `sudo timedatectl set-timezone Asia/Beirut`.
- **CSO/SNU/OSO register values initially backwards** in code — fixed and verified against real hardware writes.
- **Charge current register: official doc's Reg 34 was wrong** for this unit's actual LCD "Program 11" — real register is **Reg 38** (Reg 93 mirrors the same value but 38 is authoritative), discovered via diff-test after a failed initial assumption.
- **Register write settling delay (~2s)** — an immediate read-back after certain writes (confirmed on Reg 38) can show the stale pre-write value; a short delay before reading resolves it.
- **Breaker trip during dynamic charge current testing** — initial safety margin (2A, ~90% breaker utilization) was insufficient against real AC inrush current and the register settling delay; increased to 4A (~80% utilization).
- **Output priority can be left stuck on UTI after manual testing** — since only Layer 1's daily run and Rule 1's active firing ever set output priority, and nothing currently de-escalates it otherwise, a manually-set UTI state (e.g. during testing) persists until the next Layer 1 run. Known limitation, not yet auto-corrected.
- **Layer 1 and Layer 2 originally ran separate decision algorithms that could silently disagree** — Layer 2's old `hours_until_critical` logic once recommended full escalation for a mere 24-minute gain in overnight buffer, and `relax_if_battery_full()` didn't know *why* Layer 1 had escalated, risking draining a proactively-built buffer. Unified into one shared `classify_energy_balance()` function (see "Unified Decision Architecture"), tested against multiple real and simulated scenarios before being trusted live.
- **Rule 1's default case originally hard-coded `OSO`** instead of `None`, silently undoing any Layer 1/2 escalation within seconds of it being set. Fixed to return `None` (no action) when it doesn't fire.

## Status

Phases 0-4 (MVP, Event Tracking, Weather/Panel Monitoring, Three-Layer Predictive Charging) complete and verified against real hardware, running live via systemd. Phase 5 (Monthly PDF Report + email delivery) complete and scheduled. Phase 6 in progress: multi-day forecast chaining and the Layer 1/2 unification (shared decision logic, escalate-only enforcement, unified `relax_if_battery_full()`) complete and tested; cumulative battery cycle tracking complete; panel cleaning detection, threshold auto-tuning, temperature-correlated load prediction, and EDL availability pattern prediction remain blocked on further real-world data. Alerts (Telegram/email for anomalies) deferred alongside the other data-gated items.
# Growatt SPF 5000 ES — Raspberry Pi Modbus Connection

## Verified Connection Settings

| Setting     | Value                          |
|-------------|---------------------------------|
| Connection  | USB (USB-A on Pi → USB-B on inverter) |
| Device path | `/dev/ttyUSB0`                  |
| Library     | `pymodbus` 3.14.0                |
| Baudrate    | 9600                             |
| Parity      | N                                 |
| Stopbits    | 1                                 |
| Bytesize    | 8                                 |
| Device ID / Slave ID | 1                        |

**Note on pymodbus 3.14 API:** this version renamed the `slave` keyword argument to `device_id`. Using `slave=` will raise `TypeError: got an unexpected keyword argument`. Use `device_id=` instead.

## Install

```bash
pip install pymodbus --break-system-packages
```

## Confirmed Working Read (Input Registers)

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
| **02** | **ChargeConfig** | **0=PV first (CSO), 1=PV&UTI (SNU), 2=PV Only** | ✅ Confirmed: register read `1` matched known SNU setting. This is LCD Program 14. |
| 03 | UtiOutStart | 0-23 (hour) | |
| 04 | UtiOutEnd | 0-23 (hour) | |
| 05 | UtiChargeStart | 0-23 (hour) | |
| 06 | UtiChargeEnd | 0-23 (hour) | |
| 34 | MaxChargeCurr | 10-130 (×1A) | Max charge current setting |
| 35 | BulkChargeVolt | 500-580 (×0.1V) | |
| 36 | FloatChargeVolt | 500-560 (×0.1V) | |
| **37** | **BatLowToUtiVolt*** | **SOC % × 10 (e.g. 500 = 50%)** | ✅ Confirmed: this is LCD **Program 12** (Battery-to-Grid SOC Gate). Doc labels it a voltage register (444-514, ×0.1V) for lead-acid systems, but on this unit (Battery Type = Lithium w/ BMS) it's repurposed to store SOC percentage instead. Verified by changing LCD 40%→50%, register moved 400→500. |
| 39 | Battery Type | 0=Lead_Acid, 1=Lithium, 2=CustomLead_Acid | |
| 45-50 | Sys Year/Month/Day/Hour/Min/Sec | | System clock — confirmed via live diff test, NOT Program 12 |
| 76-77 | Rate Watt (H/L) | ×0.1W | Rated active power |

*Register name kept from the official doc for traceability, despite the repurposed meaning on this firmware/battery-type combination.

## Confirmed Input Registers — Load & AC Input Power

| Reg | Name | Scale | Use |
|-----|------|-------|-----|
| 9-10 | Output Active Power (H/L) | ÷10 (W) | ✅ `load_power` — confirmed against Load Percent (reg 27) |
| 27 | Load Percent | ÷10 (%) | Cross-check reference for load_power |
| 36-37 | AC Input Watt (H/L) | ÷10 (W) | Power currently being drawn from EDL |
| 20 | Grid Volt (AC input voltage) | ÷10 (V) | ✅ `edl_present` = (value > ~100) — confirmed reads 0 when EDL off |

## Notes on Protocol Limits (from official doc)

- Baud rate: 9600 bps (confirmed working)
- Minimum command period: 850ms between requests — don't poll faster than this
- **Max read/write length: 45 registers per request** (not 125 as generic Modbus allows — Growatt-specific limit)
- Reference: [Growatt OffGrid SPF5000 Modbus RS485 RTU Protocol V0.11](https://watts247.com/manuals/gw/GrowattModBusProtocol.pdf)

## Status

All Phase 0 required registers (read + write) are now confirmed against real hardware. Ready for Issue 3: basic polling script.
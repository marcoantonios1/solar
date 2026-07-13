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

## Still To Do

- [ ] Identify exact register for `load_power` (total house draw) — likely Output Active Power (9-10) or Load Percent (27) × rated power
- [ ] Identify exact register/bit for `edl_present` (AC input live status) — compare register dumps with EDL on vs. off to isolate
- [ ] Confirm holding register addresses for Program 01 (Output Priority), Program 12 (Battery-to-Grid SOC gate), Program 14 (Charger Priority) — needed for write access
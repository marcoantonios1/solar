import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymodbus.client import ModbusSerialClient
from config_loader import config
from tests.test_irradiance import get_clearsky_poa_irradiance

RATED_WATTS = config["panels"]["rated_watts"] * config["panels"]["count"]
DEVICE_ID = config["modbus"]["device_id"]


def get_actual_pv_power():
    client = ModbusSerialClient(
        port=config["modbus"]["port"],
        baudrate=config["modbus"]["baudrate"],
        parity=config["modbus"]["parity"],
        stopbits=config["modbus"]["stopbits"],
        bytesize=config["modbus"]["bytesize"],
        timeout=3
    )
    if not client.connect():
        return None
    result = client.read_input_registers(3, count=2, device_id=DEVICE_ID)
    client.close()
    if result.isError():
        return None
    return result.registers[1] / 10


if __name__ == "__main__":
    irradiance = get_clearsky_poa_irradiance()
    poa = irradiance["poa_global"]
    expected_power = RATED_WATTS * (poa / 1000)

    actual_power = get_actual_pv_power()

    print(f"Clear-sky POA irradiance: {poa:.1f} W/m^2")
    print(f"Expected power (STC-based): {expected_power:.1f} W")
    if actual_power is not None:
        print(f"Actual measured power: {actual_power:.1f} W")
        if expected_power > 0:
            ratio = (actual_power / expected_power) * 100
            print(f"Actual as % of expected: {ratio:.1f}%")
    else:
        print("Could not read actual power (port may be locked by the running service).")
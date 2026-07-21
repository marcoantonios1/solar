import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymodbus.client import ModbusSerialClient
from config_loader import config
from weather import fetch_current_weather
from tests.test_irradiance import get_expected_power

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
    weather = fetch_current_weather()
    ambient_temp = weather["ambient_temp_c"] if weather else 25

    result = get_expected_power(ambient_temp_c=ambient_temp)
    expected_power = result["expected_power_w"]

    actual_power = get_actual_pv_power()

    print(f"Ambient temp: {ambient_temp} C")
    print(f"Obstructed: {result['obstructed']}")
    print(f"POA irradiance: {result['poa_irradiance']:.1f} W/m^2")
    print(f"Panel temp estimate: {result['panel_temp_c']:.1f} C")
    print(f"Temp derating: {result['temp_factor']:.4f}")
    print(f"Age degradation ({result['years_since_install']:.1f} yrs): {result['degradation_factor']:.4f}")
    print(f"Expected power (full model): {expected_power:.1f} W")
    if actual_power is not None:
        print(f"Actual measured power: {actual_power:.1f} W")
        if expected_power > 0:
            ratio = (actual_power / expected_power) * 100
            print(f"Actual as % of expected: {ratio:.1f}%")
    else:
        print("Could not read actual power (port may be locked by the running service).")
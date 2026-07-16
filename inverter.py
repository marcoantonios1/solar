from pymodbus.client import ModbusSerialClient
import glob
import time

from config_loader import config

DEVICE_ID = config["modbus"]["device_id"]
CHARGER_PRIORITY_REG = config["registers"]["charger_priority"]["address"]

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

CSO = 0
SNU = 1
OSO = 2


def mode_name(mode_value):
    return {CSO: "CSO", SNU: "SNU", OSO: "OSO"}.get(mode_value, str(mode_value))


def make_client(port):
    return ModbusSerialClient(
        port=port,
        baudrate=config["modbus"]["baudrate"],
        parity=config["modbus"]["parity"],
        stopbits=config["modbus"]["stopbits"],
        bytesize=config["modbus"]["bytesize"],
        timeout=3
    )


def find_inverter_port():
    candidates = sorted(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*'))

    for candidate in candidates:
        test_client = make_client(candidate)
        try:
            if test_client.connect():
                result = test_client.read_input_registers(18, count=1, device_id=DEVICE_ID)
                if not result.isError():
                    print(f"Found inverter on {candidate}")
                    return candidate
        except Exception:
            pass
        finally:
            try:
                test_client.close()
            except Exception:
                pass

    print(f"No responsive device found among {candidates}, falling back to config.json port.")
    return config["modbus"].get("port")


def read_values_once(client):
    from datetime import datetime
    try:
        pv_result = client.read_input_registers(3, count=2, device_id=DEVICE_ID)
        bat_result = client.read_input_registers(18, count=1, device_id=DEVICE_ID)
        load_result = client.read_input_registers(9, count=2, device_id=DEVICE_ID)
        grid_result = client.read_input_registers(20, count=1, device_id=DEVICE_ID)

        if any(r.isError() for r in [pv_result, bat_result, load_result, grid_result]):
            return None

        pv_power = pv_result.registers[1] / 10
        battery_soc = bat_result.registers[0]
        load_power = load_result.registers[1] / 10
        grid_voltage = grid_result.registers[0] / 10
        edl_present = grid_voltage > 100

        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "pv_power": pv_power,
            "battery_soc": battery_soc,
            "load_power": load_power,
            "edl_present": edl_present,
        }
    except Exception as e:
        print(f"Read error: {e}")
        return None


def read_values_with_retry(client_holder):
    for attempt in range(1, MAX_RETRIES + 1):
        values = read_values_once(client_holder[0])
        if values is not None:
            return values
        print(f"Read attempt {attempt}/{MAX_RETRIES} failed.")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)

    print("All retries failed. Attempting to re-detect the inverter's port...")

    try:
        client_holder[0].close()
    except Exception:
        pass

    new_port = None
    for scan_attempt in range(1, 4):
        time.sleep(3)
        new_port = find_inverter_port()
        if new_port:
            break
        print(f"Port scan attempt {scan_attempt}/3 found nothing, retrying...")

    if not new_port:
        print("Could not find inverter after rescanning. Will retry next cycle.")
        return None

    print(f"Reconnecting on {new_port}...")
    client_holder[0] = make_client(new_port)
    client_holder[0].connect()
    return read_values_once(client_holder[0])


def read_current_charger_mode_once(client):
    try:
        result = client.read_holding_registers(CHARGER_PRIORITY_REG, count=1, device_id=DEVICE_ID)
        if result.isError():
            return None
        return result.registers[0]
    except Exception as e:
        print(f"Mode read error: {e}")
        return None


def read_current_charger_mode_with_retry(client):
    for attempt in range(1, MAX_RETRIES + 1):
        mode = read_current_charger_mode_once(client)
        if mode is not None:
            return mode
        print(f"Mode read attempt {attempt}/{MAX_RETRIES} failed.")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)
    return None


def set_charger_mode(client, mode_value):
    try:
        result = client.write_register(CHARGER_PRIORITY_REG, mode_value, device_id=DEVICE_ID)
        return not result.isError()
    except Exception as e:
        print(f"Mode write error: {e}")
        return False
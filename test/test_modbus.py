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
    print("Connected to inverter.")

    result = client.read_holding_registers(0, count=1, device_id=1)

    if result.isError():
        print("Got a response, but it's an error:", result)
    else:
        print("Success! Register 0 value:", result.registers)

    client.close()
else:
    print("Could not connect - check port, cable, and permissions.")
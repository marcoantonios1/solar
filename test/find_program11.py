from pymodbus.client import ModbusSerialClient

client = ModbusSerialClient(
    port='/dev/ttyUSB0',
    baudrate=9600,
    parity='N',
    stopbits=1,
    bytesize=8,
    timeout=3
)

def dump_holding(client):
    values = {}
    CHUNK = 45
    for start in range(0, 200, CHUNK):
        n = min(CHUNK, 200 - start)
        result = client.read_holding_registers(start, count=n, device_id=1)
        if result.isError():
            continue
        for i, raw in enumerate(result.registers):
            values[start + i] = raw
    return values

if client.connect():
    input("Note the CURRENT Program 11 value on the LCD, then press Enter to take a baseline reading...")
    before = dump_holding(client)

    input("Now go change Program 11 on the LCD to a DIFFERENT value, then press Enter here...")
    after = dump_holding(client)

    print("\n--- Registers that changed ---")
    for addr in before:
        if before[addr] != after.get(addr):
            print(f"Reg {addr}: {before[addr]} -> {after.get(addr)}")

    client.close()
else:
    print("Could not connect.")
"""Discover GATT services and characteristics on the MAGRGB"""
import asyncio
from bleak import BleakClient, BleakScanner

# EDIT THIS: set to the HAP-BLE MAC address of your MAGRGB strip.
# Run scan.py to find it — look for the address with Manufacturer ID 76 (Apple).
DEVICE_MAC = "XX:XX:XX:XX:XX:XX"

async def main():
    print(f"Connecting to {DEVICE_MAC}...")
    async with BleakClient(DEVICE_MAC) as client:
        print(f"Connected!\n")
        for service in client.services:
            print(f"Service: {service.uuid}  ({service.description})")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"  Char: {char.uuid}  handle=0x{char.handle:04x}  [{props}]")
                if "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char.uuid)
                        print(f"    Value: {val.hex()}  ({val})")
                    except Exception as e:
                        print(f"    Read error: {e}")

asyncio.run(main())

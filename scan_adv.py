"""Show raw advertisement data for all MAGRGB devices"""
import asyncio
from bleak import BleakScanner

TARGET = "MAGRGB"

def callback(device, adv):
    if TARGET.lower() in (device.name or "").lower():
        print(f"\nDevice: {device.name}  [{device.address}]")
        print(f"  RSSI: {adv.rssi}")
        print(f"  Service UUIDs: {adv.service_uuids}")
        print(f"  Service Data:  {adv.service_data}")
        print(f"  Manufacturer:  { {k: v.hex() for k, v in adv.manufacturer_data.items()} }")

async def main():
    print(f"Scanning for MAGRGB devices...")
    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    await asyncio.sleep(12)
    await scanner.stop()
    print("\nDone.")

asyncio.run(main())

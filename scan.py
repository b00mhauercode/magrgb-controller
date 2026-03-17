"""Scan for BLE devices and print names + MACs"""
import asyncio
from bleak import BleakScanner

async def scan():
    print("Scanning for 10 seconds...")
    devices = await BleakScanner.discover(timeout=10)
    for d in sorted(devices, key=lambda x: x.name or ""):
        print(f"  {d.address}  {d.name or '(no name)'}")

asyncio.run(scan())

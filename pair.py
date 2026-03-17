"""
One-time pairing script for Secretlab MAGRGB.
Bypasses aiohomekit's advertisement-based discovery (the MAGRGB uses
Nanoleaf's company ID, not Apple's, so the normal scanner ignores it).
Connects directly by MAC and performs the HAP-BLE SRP pairing.

Run this once to create pairing.json, then use magnus_wled_bridge.py.

Usage:
    python pair.py XXX-XX-XXX
"""
import asyncio
import json
import logging
import os
import sys
import uuid

from bleak import BleakScanner

from aiohomekit.controller.ble.bleak import AIOHomeKitBleakClient
from aiohomekit.controller.ble.client import drive_pairing_state_machine
from aiohomekit.model import CharacteristicsTypes
from aiohomekit.protocol import perform_pair_setup_part1, perform_pair_setup_part2

logging.basicConfig(level=logging.WARNING)

# EDIT THIS: set to the HAP-BLE MAC address of your MAGRGB strip.
# Run scan.py to find it — look for the address with Manufacturer ID 76 (Apple).
DEVICE_MAC   = "XX:XX:XX:XX:XX:XX"
ALIAS        = "magrgb"
PAIRING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pairing.json")


async def main(setup_code: str):
    # Scan to get the BLEDevice object (bleak needs this for connection metadata)
    print(f"Scanning for {DEVICE_MAC}...")
    device = await BleakScanner.find_device_by_address(DEVICE_MAC, timeout=15)
    if not device:
        print(f"ERROR: Device {DEVICE_MAC} not found. Is the strip powered on?")
        return
    print(f"Found: {device.name}  [{device.address}]")

    # Connect using aiohomekit's BLE client
    print("Connecting...")
    client = AIOHomeKitBleakClient(device)
    await client.connect()
    print("Connected!")

    try:
        # Part 1: SRP init — device sends salt + public key
        print("Starting HAP pairing (part 1)...")
        salt, pub_key = await drive_pairing_state_machine(
            client,
            CharacteristicsTypes.PAIR_SETUP,
            perform_pair_setup_part1(with_auth=False),
        )

        # Part 2: SRP verify — we prove we know the PIN, get long-term keys back
        print(f"Finishing HAP pairing (part 2) with code {setup_code}...")
        pairing_data = await drive_pairing_state_machine(
            client,
            CharacteristicsTypes.PAIR_SETUP,
            perform_pair_setup_part2(
                setup_code,
                str(uuid.uuid4()),
                salt,
                pub_key,
            ),
        )

        pairing_data["AccessoryAddress"] = DEVICE_MAC
        pairing_data["Connection"] = "BLE"

        data = {ALIAS: pairing_data}
        with open(PAIRING_FILE, "w") as f:
            json.dump(data, f, indent=2)

        print(f"\nPairing successful!")
        print(f"Accessory Pairing ID: {pairing_data.get('AccessoryPairingID')}")
        print(f"Saved to {PAIRING_FILE}")

    finally:
        await client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python pair.py XXX-XX-XXX")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))

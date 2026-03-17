"""
Quick test — cycle through colors to verify HAP-BLE control works.
Usage: python test.py
"""
import asyncio
import colorsys
import json
import os

from aiohomekit.characteristic_cache import CharacteristicCacheMemory
from aiohomekit.controller.ble.controller import BleController
from aiohomekit.model import Accessories, AccessoriesState

from compat import CompatBleakScanner

# EDIT THIS: set to the HAP-BLE MAC address of your MAGRGB strip.
# Run scan.py to find it — look for the address with Manufacturer ID 76 (Apple).
DEVICE_MAC   = "XX:XX:XX:XX:XX:XX"
PAIRING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pairing.json")
ALIAS        = "magrgb"


def rgb_to_hsv(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
    return round(h * 360), round(s * 100), round(v * 100)


async def main():
    with open(PAIRING_FILE) as f:
        pairing_data = json.load(f)[ALIAS]

    scanner = CompatBleakScanner()
    controller = BleController(char_cache=CharacteristicCacheMemory(), bleak_scanner_instance=scanner)
    await controller.async_start()

    # Load pairing into controller — registers it so scanner updates populate description
    pairing = controller.load_pairing(ALIAS, pairing_data)

    # Pre-initialize accessories state so advertisement callbacks don't crash
    pairing._accessories_state = AccessoriesState(Accessories(), 0, None, 0)

    # Wait for device advertisement to populate the description
    print(f"Waiting for {DEVICE_MAC} advertisement...")
    for _ in range(20):
        await asyncio.sleep(1)
        if pairing.description is not None:
            print(f"Found: {pairing.description.name}")
            break
    else:
        print("Device not found in advertisements. Trying anyway...")

    # List accessories
    print("\nListing accessories...")
    accessories = await pairing.list_accessories_and_characteristics()

    on_iid = hue_iid = sat_iid = bri_iid = None
    aid = accessories[0]["aid"]
    for svc in accessories[0]["services"]:
        for char in svc["characteristics"]:
            t = char["type"].upper()
            if "00000025" in t: on_iid  = char["iid"]; print(f"  On/Off  iid={on_iid}")
            if "00000013" in t: hue_iid = char["iid"]; print(f"  Hue     iid={hue_iid}")
            if "0000002F" in t: sat_iid = char["iid"]; print(f"  Sat     iid={sat_iid}")
            if "00000008" in t: bri_iid = char["iid"]; print(f"  Bright  iid={bri_iid}")

    if not all([on_iid, hue_iid, sat_iid, bri_iid]):
        print("\nCouldn't find all lightbulb characteristics. Raw dump:")
        for svc in accessories[0]["services"]:
            print(f"  Service {svc['type']}")
            for char in svc["characteristics"]:
                print(f"    {char['type']}  iid={char['iid']}  perms={char.get('perms')}")
        return

    print(f"\nTurning on — RED")
    h, s, v = rgb_to_hsv(255, 0, 0)
    await pairing.put_characteristics([(aid, on_iid, True), (aid, hue_iid, h), (aid, sat_iid, s), (aid, bri_iid, v)])
    await asyncio.sleep(2)

    print("GREEN")
    h, s, v = rgb_to_hsv(0, 255, 0)
    await pairing.put_characteristics([(aid, hue_iid, h), (aid, sat_iid, s), (aid, bri_iid, v)])
    await asyncio.sleep(2)

    print("BLUE")
    h, s, v = rgb_to_hsv(0, 0, 255)
    await pairing.put_characteristics([(aid, hue_iid, h), (aid, sat_iid, s), (aid, bri_iid, v)])
    await asyncio.sleep(2)

    print("WHITE 50%")
    await pairing.put_characteristics([(aid, hue_iid, 0), (aid, sat_iid, 0), (aid, bri_iid, 50)])
    await asyncio.sleep(2)

    print("OFF")
    await pairing.put_characteristics([(aid, on_iid, False)])

    await pairing.shutdown()
    await controller.async_stop()
    print("\nDone!")

asyncio.run(main())

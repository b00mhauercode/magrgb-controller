"""Bleak 2.x compatibility shim for aiohomekit's BleController.

aiohomekit's BleController expects a scanner instance with the old-style
register_detection_callback() interface. This wrapper adapts the current
BleakScanner constructor API to that interface.
"""
from bleak import BleakScanner


class CompatBleakScanner:
    """Adapts BleakScanner to the interface expected by aiohomekit's BleController."""

    def __init__(self):
        self._scanner = None

    def register_detection_callback(self, callback):
        self._scanner = BleakScanner(detection_callback=callback)

    async def start(self):
        if self._scanner:
            await self._scanner.start()

    async def stop(self):
        if self._scanner:
            await self._scanner.stop()

    @property
    def discovered_devices_and_advertisement_data(self):
        return self._scanner.discovered_devices_and_advertisement_data if self._scanner else {}

"""
Magnus WLED Bridge
Presents the Secretlab MAGRGB as a WLED device so SignalRGB's built-in
WLED integration can discover and stream colors to it via HAP-BLE.

Usage:
  Run as Administrator:  python magnus_wled_bridge.py
  (Port 80 requires admin on Windows)

In SignalRGB:
  Home -> Lighting Services -> WLED -> "Discover WLED device by IP" -> 127.0.0.2
  Press Enter -> "Magnus RGB Strip" will appear -> Link it.
"""
import asyncio
import colorsys
import json
import os
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from aiohomekit.characteristic_cache import CharacteristicCacheMemory
from aiohomekit.controller.ble.controller import BleController
from aiohomekit.model import Accessories, AccessoriesState

from compat import CompatBleakScanner

# ── Configuration ──────────────────────────────────────────────────────────────
try:
    from config_local import DEVICE_MAC
except ImportError:
    from config import DEVICE_MAC
PAIRING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pairing.json")
ALIAS        = "magrgb"
WLED_MAC     = DEVICE_MAC.replace(":", "")   # derived — no need to edit separately

# HAP characteristic IIDs
AID     = 1
IID_ON  = 51   # On/Off bool
IID_BRI = 52   # Brightness 0-100
IID_HUE = 53   # Hue 0-360
IID_SAT = 54   # Saturation 0-100

# Zone count for pixel averaging (kept for UDP handler)
NUM_ZONES = 60

HTTP_PORT     = 80
UDP_PORT      = 21325
SEND_INTERVAL = 0.05   # 20 Hz — empirically faster, stable on good BLE link
# ──────────────────────────────────────────────────────────────────────────────


# ── Shared color state ─────────────────────────────────────────────────────────

_lock          = threading.Lock()
_pending_zones = None   # list of NUM_ZONES (r, g, b) tuples, or None
_global_bri    = 100    # 0-100
_udp_count     = 0      # debug: total UDP frames received


def set_zones(zones):
    global _pending_zones
    with _lock:
        _pending_zones = zones


def set_brightness(bri_0_100):
    global _global_bri
    with _lock:
        _global_bri = max(0, min(100, bri_0_100))


# ── WLED JSON responses ────────────────────────────────────────────────────────

WLED_INFO = {
    "ver": "0.14.0", "vid": 2310130,
    "leds": {"count": 123, "pwr": 0, "fps": 30, "maxpwr": 5, "maxseg": 32,
             "seglc": [123], "lc": 123, "rgbw": False, "wv": 0, "cct": 0},
    "str": False, "name": "Magnus RGB Strip", "udpport": UDP_PORT,
    "live": False, "lm": "", "lip": "", "ws": 0,
    "fxcount": 118, "palcount": 71, "cpalcount": 0,
    "wifi": {"bssid": "00:00:00:00:00:00", "rssi": -50, "signal": 100, "channel": 1},
    "fs": {"u": 0, "t": 0, "pj": 0}, "ndc": 0,
    "arch": "esp32", "core": "v3.3.6", "lwip": 2,
    "freeheap": 100000, "uptime": 1000, "opt": 131,
    "brand": "WLED", "product": "FOSS",
    "mac": WLED_MAC, "ip": "127.0.0.2",
}

WLED_STATE = {
    "on": True, "bri": 255, "transition": 7, "ps": -1, "pl": -1,
    "nl": {"on": False, "dur": 60, "mode": 1, "tbri": 0, "rem": -1},
    "udpn": {"send": False, "recv": False, "sgrp": 0, "rgrp": 0},
    "lor": 0, "mainseg": 0,
    "seg": [{"id": 0, "start": 0, "stop": 123, "len": 123,
             "grp": 1, "spc": 0, "of": 0, "on": True, "frz": False,
             "bri": 255, "cct": 127, "set": 0,
             "col": [[255, 255, 255, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
             "fx": 0, "sx": 128, "ix": 128, "pal": 0,
             "c1": 128, "c2": 128, "c3": 16,
             "sel": True, "rev": False, "mi": False,
             "o1": False, "o2": False, "o3": False, "si": 0, "m12": 0}],
}


# ── HTTP handler ───────────────────────────────────────────────────────────────

class WLEDHttpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path in ("/json/info", "/json"):
            body = json.dumps({"state": WLED_STATE, "info": WLED_INFO} if path == "/json"
                              else WLED_INFO).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/json/state":
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            if length > 4096:
                self.send_response(413); self.end_headers(); return
            try:
                data = json.loads(self.rfile.read(length))
                if "bri" in data:
                    set_brightness(round(data["bri"] / 255 * 100))
                    WLED_STATE["bri"] = data["bri"]
                if data.get("on") is False:
                    set_zones([(0, 0, 0)] * NUM_ZONES)
            except Exception as e:
                print(f"  HTTP POST parse error: {e}")
            resp = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, fmt, *args):
        print(f"  HTTP {args[0]} {args[1]}")


# ── UDP handler ────────────────────────────────────────────────────────────────

class WLEDUdpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        global _udp_count
        data = self.request[0]
        if len(data) < 7:
            return
        if data[0] == 0x04:          # DRGB — 3 bytes per pixel after 4-byte header
            n = min(123, (len(data) - 4) // 3)
            if n > 0:
                zones = []
                for z in range(NUM_ZONES):
                    lo = int(z * n / NUM_ZONES)
                    hi = max(lo + 1, int((z + 1) * n / NUM_ZONES))
                    hi = min(hi, n)
                    count = hi - lo
                    r = round(sum(data[4 + (lo+i)*3]   for i in range(count)) / count)
                    g = round(sum(data[4 + (lo+i)*3+1] for i in range(count)) / count)
                    b = round(sum(data[4 + (lo+i)*3+2] for i in range(count)) / count)
                    zones.append((r, g, b))
                _udp_count += 1
                if _udp_count % 30 == 1:   # log every 30 frames (~1/sec at 30fps)
                    r0, g0, b0 = zones[0]
                    print(f"  UDP frame #{_udp_count}  z0=rgb({r0},{g0},{b0})")
                set_zones(zones)
        else:
            print(f"  UDP unknown protocol byte: 0x{data[0]:02x} (len={len(data)})")


# ── Animation helpers ──────────────────────────────────────────────────────────

def rgb_to_hapsv(r, g, b):
    """RGB (0-255) → (hue 0-360, sat 0-100, bri 0-100)"""
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return round(h * 360), round(s * 100), round(v * 100)


# ── HAP-BLE loop ───────────────────────────────────────────────────────────────

async def hap_loop():
    """Main asyncio loop: averages all zones to one color, writes via lightbulb characteristics."""

    with open(PAIRING_FILE) as f:
        pairing_data = json.load(f)[ALIAS]

    scanner = CompatBleakScanner()
    controller = BleController(
        char_cache=CharacteristicCacheMemory(),
        bleak_scanner_instance=scanner,
    )
    await controller.async_start()

    pairing = controller.load_pairing(ALIAS, pairing_data)
    pairing._accessories_state = AccessoriesState(Accessories(), 0, None, 0)

    print(f"Waiting for {DEVICE_MAC}...")
    for _ in range(30):
        await asyncio.sleep(1)
        if pairing.description is not None:
            print(f"Found: {pairing.description.name}")
            break
    else:
        print("WARNING: Device not found in scan, attempting connection anyway...")

    print("Startup: turning on...")
    await pairing.put_characteristics([(AID, IID_ON, True)])
    await asyncio.sleep(0.5)
    print("Ready.\n")

    last_time = 0.0
    last_rgb  = None   # deduplicate full rgb — gate whether we write at all
    last_h    = None   # per-channel dedup — only write channels that changed
    last_s    = None
    last_v    = None
    strip_on  = True

    while True:
        try:
            now = asyncio.get_running_loop().time()
            with _lock:
                zones = _pending_zones
                bri   = _global_bri

            if zones is not None and (now - last_time) >= SEND_INTERVAL:
                n = len(zones)
                r = round(sum(z[0] for z in zones) / n)
                g = round(sum(z[1] for z in zones) / n)
                b = round(sum(z[2] for z in zones) / n)

                is_off = (r == 0 and g == 0 and b == 0)

                if is_off:
                    if strip_on:
                        await pairing.put_characteristics([(AID, IID_ON, False)])
                        strip_on = False
                        last_rgb = last_h = last_s = last_v = None
                        last_time = now
                        print("  HAP -> OFF")
                elif (r, g, b) != last_rgb:
                    h, s, v = rgb_to_hapsv(r, g, b)
                    v_scaled = round(v * bri / 100)

                    # Only include characteristics that actually changed
                    h_changed = h       != last_h
                    s_changed = s       != last_s
                    v_changed = v_scaled != last_v

                    writes = []
                    if not strip_on:
                        writes.append((AID, IID_ON, True))
                        strip_on = True
                    if h_changed:
                        writes.append((AID, IID_HUE, h))
                    if s_changed:
                        writes.append((AID, IID_SAT, s))
                    if v_changed:
                        writes.append((AID, IID_BRI, v_scaled))

                    if writes:
                        await pairing.put_characteristics(writes)
                        last_h = h
                        last_s = s
                        last_v = v_scaled
                    last_rgb  = (r, g, b)
                    last_time = now
                    changed = ",".join(
                        c for c, w in [("H", h_changed), ("S", s_changed), ("B", v_changed)]
                        if w
                    ) or "on"
                    print(f"  HAP -> hsv({h},{s},{v_scaled})  rgb({r},{g},{b})  [{changed}]")

        except Exception as e:
            print(f"HAP loop error: {e}")
            await asyncio.sleep(2)

        await asyncio.sleep(0.02)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    """Start UDP and HTTP servers, then run the HAP-BLE loop."""
    udp_server = socketserver.UDPServer(("127.0.0.2", UDP_PORT), WLEDUdpHandler)
    threading.Thread(target=udp_server.serve_forever, daemon=True).start()
    print(f"WLED UDP on 127.0.0.2:{UDP_PORT}")

    try:
        http_server = HTTPServer(("127.0.0.2", HTTP_PORT), WLEDHttpHandler)
        threading.Thread(target=http_server.serve_forever, daemon=True).start()
        print(f"WLED HTTP on :{HTTP_PORT}")
    except PermissionError:
        user = os.environ.get("USERNAME", "Everyone")
        print(f"\nERROR: Port 80 requires Administrator.")
        print(f"  Option A: Right-click terminal -> 'Run as administrator'")
        print(f"  Option B (one-time): run in admin prompt then re-run normally:")
        print(f"    netsh http add urlacl url=http://127.0.0.2:80/ user={user}")
        sys.exit(1)

    print()
    print("In SignalRGB: Lighting Services -> WLED -> IP: 127.0.0.2")
    print("Ctrl+C to quit\n")

    try:
        asyncio.run(hap_loop())
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        udp_server.shutdown()
        http_server.shutdown()


if __name__ == "__main__":
    main()

"""
Magnus WLED Bridge — Thread/UDP edition
Presents the Secretlab MAGRGB as a WLED device so SignalRGB's built-in
WLED integration can discover and stream colors to it.

Colors are streamed directly to the device via UDP port 60222 (Nanoleaf
external control protocol) over the Thread/IPv6 network — no HAP-BLE
pairing required.

Transport: Thread UDP (~20 Hz, single averaged color)
vs. HAP-BLE (magnus_wled_bridge_hapble.py): ~10 Hz, single color, needs pairing

Requirements:
  - Device must be reachable at its Thread IPv6 address (ping it to check)
  - Nanoleaf Desktop app must be running on first launch to call
    enableStreamControl. After that it can be closed — the device holds
    streaming mode until power-cycled.
  - DEVICE_INFO must be set in config_local.py (see config.py for template)

Usage:
  Run as Administrator:  python magnus_wled_bridge_thread.py
  (Port 80 requires admin on Windows)

In SignalRGB:
  Home -> Lighting Services -> WLED -> "Discover WLED device by IP" -> 127.0.0.2
  Press Enter -> "Magnus RGB Strip" will appear -> Link it.
"""
import asyncio
import json
import os
import socket
import socketserver
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

try:
    from config_local import DEVICE_INFO, DEVICE_MAC
except ImportError:
    from config import DEVICE_INFO, DEVICE_MAC

# ── Configuration ──────────────────────────────────────────────────────────────

DEVICE_IP    = DEVICE_INFO["ip"]         # Thread IPv6 address
NANOLEAF_API = "http://127.0.0.1:15765"  # Nanoleaf Desktop local service
NUM_ZONES    = 60
HTTP_PORT    = 80
UDP_PORT     = 21325
STREAM_PORT  = 60222    # Nanoleaf external control UDP port on device
SEND_INTERVAL = 0.05    # 20 Hz

WLED_MAC = DEVICE_MAC.replace(":", "")
# ──────────────────────────────────────────────────────────────────────────────


# ── Shared color state ─────────────────────────────────────────────────────────

_lock          = threading.Lock()
_pending_zones = None
_global_bri    = 100
_udp_count     = 0


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


# ── WLED UDP handler ───────────────────────────────────────────────────────────

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
                if _udp_count % 30 == 1:
                    r0, g0, b0 = zones[0]
                    print(f"  UDP frame #{_udp_count}  z0=rgb({r0},{g0},{b0})")
                set_zones(zones)
        else:
            print(f"  UDP unknown protocol byte: 0x{data[0]:02x} (len={len(data)})")


# ── Nanoleaf external control (Thread UDP streaming) ──────────────────────────

def enable_stream_control():
    """Ask Nanoleaf Desktop to put the device in external control mode."""
    device = {**DEVICE_INFO,
              "shapeType": 2,
              "scaleFactor": 3.195266272189349,
              "panels": [{"panelID": 0, "centroidX": 1082, "centroidY": 1921}]}
    body = json.dumps({"devices": [device]}).encode()
    req = urllib.request.Request(
        f"{NANOLEAF_API}/essentials/enableStreamControl",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        return resp.read()


def make_stream_packet(r, g, b):
    """Build a single-panel Nanoleaf external control UDP packet."""
    # [numPanels(2)] + [panelID(2) R G B W transTime(2)] per panel
    return bytes([0, 1,        # 1 panel
                  0, 0,        # panelID 0
                  r, g, b, 0,  # RGB + white channel
                  0, 1])       # transitionTime = 1 (fast)


# ── Stream loop ────────────────────────────────────────────────────────────────

async def stream_loop():
    """Reads pending zones, averages to one color, streams via Thread UDP."""

    print("Enabling stream control via Nanoleaf Desktop...")
    try:
        resp = enable_stream_control()
        print(f"  OK: {resp}")
    except urllib.error.URLError:
        print("  WARNING: Nanoleaf Desktop app not running.")
        print("  Continuing — device may still be in stream mode from a previous run.\n")

    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    print(f"Streaming to [{DEVICE_IP}]:{STREAM_PORT}")
    print("Ready.\n")

    loop       = asyncio.get_running_loop()
    last_time  = 0.0
    last_color = None

    while True:
        try:
            now = loop.time()
            with _lock:
                zones = _pending_zones
                bri   = _global_bri

            if zones is not None and (now - last_time) >= SEND_INTERVAL:
                n = len(zones)
                r = round(sum(z[0] for z in zones) / n * bri / 100)
                g = round(sum(z[1] for z in zones) / n * bri / 100)
                b = round(sum(z[2] for z in zones) / n * bri / 100)
                color = (r, g, b)

                if color != last_color:
                    pkt = make_stream_packet(r, g, b)
                    sock.sendto(pkt, (DEVICE_IP, STREAM_PORT, 0, 0))
                    last_color = color
                    last_time  = now
                    print(f"  Thread -> rgb({r},{g},{b})")

        except Exception as e:
            print(f"Stream loop error: {e}")
            await asyncio.sleep(1)

        await asyncio.sleep(0.01)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
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
        asyncio.run(stream_loop())
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        udp_server.shutdown()
        http_server.shutdown()


if __name__ == "__main__":
    main()

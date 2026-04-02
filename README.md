# Secretlab MAGRGB Controller

> Full Python controller, SignalRGB integration, and technical protocol documentation for the **Secretlab Magnus XL RGB Strip** (co-developed with Nanoleaf, sold as MAGRGB).

Two bridges are included — pick the one that suits your setup:

| Bridge | Transport | Speed | Requires |
|---|---|---|---|
| `magnus_wled_bridge_thread.py` | Thread/UDP over IPv6 | ~20 Hz | Nanoleaf Desktop app (once at startup) |
| `magnus_wled_bridge_hapble.py` | HAP-BLE (encrypted GATT) | ~10 Hz | HAP pairing (`pair.py`) |

**Recommended: Thread/UDP** — faster, no BLE pairing needed, no `pairing.json` required, and the Nanoleaf Desktop app only needs to be running once to enable streaming mode.

**Use HAP-BLE if:** you don't have an Apple TV or other Thread Border Router, can't get the Thread path working, or don't want to install the Nanoleaf Desktop app. It only requires a Bluetooth adapter and the device's HomeKit setup code.

---

## Table of Contents

1. [Device Identification](#device-identification)
2. [The Reverse Engineering Journey](#the-reverse-engineering-journey)
3. [Protocol Specification](#protocol-specification)
4. [Initial Setup — Thread/UDP (recommended)](#initial-setup--threadudp-recommended)
5. [Initial Setup — HAP-BLE (fallback)](#initial-setup--hap-ble-fallback)
6. [Script Reference](#script-reference)
7. [SignalRGB Integration](#signalrgb-integration)
8. [Architecture](#architecture)
9. [Files in This Repo](#files-in-this-repo)
10. [Future Work](#future-work)
11. [Troubleshooting](#troubleshooting)
12. [Legal](#legal)

---

## Device Identification

| Field | Value |
|---|---|
| Product | Secretlab Magnus XL RGB Desk Strip |
| OEM | Nanoleaf (sold as MAGRGB) |
| BLE Advertised Name | `Secretlab MAGRGB XXBJ` |
| Protocol | HAP-BLE (Apple HomeKit Accessory Protocol over Bluetooth LE) |
| Power | USB |

### BLE Advertisement

After factory reset the device advertises on **two simultaneous addresses** — one per protocol:

| Address | Manufacturer ID | Protocol | Purpose |
|---|---|---|---|
| `XX:XX:XX:XX:XX:XX` | `76` (Apple) | HAP-BLE | HomeKit pairing + control |
| `YY:YY:YY:YY:YY:YY` | `2059` (Nanoleaf) | LTPDU | Nanoleaf app + Thread control |

> **Note:** Both addresses change after each factory reset. Use `scan.py` to find the current ones.

### HAP-BLE GATT Services

| Service UUID | Purpose |
|---|---|
| `00001800-0000-1000-8000-00805f9b34fb` | Generic Access (device name) |
| `0000003e-0000-1000-8000-0026bb765291` | HAP Accessory Information |
| `00000055-0000-1000-8000-0026bb765291` | HAP Pairing (Pair-Setup `0x4C`, Pair-Verify `0x4E`) |
| `00000043-0000-1000-8000-0026bb765291` | **HAP Lightbulb** — color control lives here |
| `00000701-0000-1000-8000-0026bb765291` | Nanoleaf scene/effect service |
| `6d2ae1c4-9aea-11ea-bb37-0242ac130002` | Nanoleaf LTPDU transport (encrypted) |

### HAP Lightbulb Characteristic IIDs

| Characteristic | HAP UUID | IID |
|---|---|---|
| On / Off | `00000025-...-0026bb765291` | 51 |
| Brightness (0–100) | `00000008-...-0026bb765291` | 52 |
| Hue (0–360°) | `00000013-...-0026bb765291` | 53 |
| Saturation (0–100%) | `0000002f-...-0026bb765291` | 54 |

---

## The Reverse Engineering Journey

### Phase 1 — Identifying the Protocol

The strip is controlled via the **Nanoleaf app** on Android/iOS, which meant the BLE traffic would tell us the protocol. The first step was an Android HCI snoop log capture.

**What the snoop log showed:**

The log captured **encrypted variable-length packets** to handles `0x008e` and `0x0090` — the actual Nanoleaf app traffic to the MAGRGB, encrypted using X25519 + AES-CTR (Nanoleaf's LTPDU protocol) or HAP-BLE ChaCha20-Poly1305. Since the keys are ephemeral per-session, these packets cannot be decrypted from the capture alone.

### Phase 2 — Understanding the Dual-Protocol Architecture

A GATT service enumeration (see `discover_services.py`) revealed the device exposes **both** HAP-BLE and Nanoleaf LTPDU service trees simultaneously:

- HAP services (`0026bb765291` UUID namespace) → Apple HomeKit protocol
- LTPDU service (`6d2ae1c4-9aea-11ea-bb37-0242ac130002`) → Nanoleaf proprietary protocol over Thread/CoAP

The device also advertises with **two manufacturer IDs** — Apple's (76) for HomeKit discovery and Nanoleaf's (2059) for the Nanoleaf app — on separate rotating BLE addresses.

This explains why aiohomekit's BLE scanner (which filters for Apple manufacturer ID 76) couldn't find the device during normal operation: it was paired and broadcasting an encrypted notification advertisement rather than a pairable one.

### Phase 3 — HAP-BLE Pairing

After factory resetting the device, the HAP address broadcasts a standard unencrypted HomeKit advertisement (manufacturer data starting with `0x06`). aiohomekit's normal discovery still couldn't find it because the advertisement used **Nanoleaf's advertisement address**, not the Apple one.

**Solution:** Bypass aiohomekit's scanner-based discovery entirely. Use `BleakScanner.find_device_by_address()` to get the BLEDevice directly, then invoke aiohomekit's low-level `drive_pairing_state_machine()` directly with the HAP Pair-Setup characteristic (`0x4C`).

The SRP pairing uses the **8-digit HomeKit setup code** printed on the device in `XXX-XX-XXX` format. The format matters — aiohomekit's `check_pin_format` rejects any other format.

**Pairing flow:**
```
1. BleakScanner.find_device_by_address(HAP_MAC)
2. AIOHomeKitBleakClient.connect()
3. drive_pairing_state_machine(PAIR_SETUP, perform_pair_setup_part1())
   → device returns SRP salt + public key
4. drive_pairing_state_machine(PAIR_SETUP, perform_pair_setup_part2(pin, uuid, salt, pubkey))
   → device returns long-term key pair (AccessoryLTPK, iOSDeviceLTSK, etc.)
5. Save pairing_data to pairing.json
```

### Phase 4 — Characteristic Control

With a valid pairing, HAP-BLE control works through aiohomekit's `BlePairing.put_characteristics()`. One compatibility issue: aiohomekit's `BlePairing` class expects to be driven by the full controller/scanner infrastructure, and crashes if `_accessories_state` is `None` when advertisement callbacks fire.

**Fix:** Pre-initialize `_accessories_state` with an empty `AccessoriesState(Accessories(), 0, None, 0)` immediately after loading the pairing.

Color is communicated in **HSV space** (not RGB), as HAP's Lightbulb service uses Hue + Saturation + Brightness as separate characteristics. RGB values from SignalRGB's WLED DRGB stream are converted with Python's `colorsys.rgb_to_hsv()`.

---

## Protocol Specification

### HAP-BLE Session

HAP-BLE uses a standard Pair-Verify handshake (X25519 + Ed25519) after pairing to establish a ChaCha20-Poly1305 encrypted session. aiohomekit handles this transparently.

### Characteristic Write Format

Control packets go through aiohomekit's `put_characteristics([(aid, iid, value)])`:

| Parameter | Value |
|---|---|
| AID | `1` (always 1 for BLE accessories) |
| IID_ON | `51` — boolean |
| IID_BRI | `52` — integer 0–100 |
| IID_HUE | `53` — integer 0–360 |
| IID_SAT | `54` — integer 0–100 |

### Color Conversion

```python
import colorsys

def rgb_to_hapsv(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return round(h * 360), round(s * 100), round(v * 100)
```

### Off vs On

HAP has a discrete On/Off characteristic (IID 51). Sending RGB `(0,0,0)` does NOT turn off the light — you must write `False` to IID_ON. The bridge handles this automatically.

---

## Initial Setup — Thread/UDP (recommended)

### Requirements

- Python 3.9+
- Windows 10/11
- **Nanoleaf Desktop app** installed and running (needed once to enable streaming mode)
- Device reachable via Thread IPv6 (it connects through an Apple TV border router)

```bash
pip install -r requirements.txt
```

### Step 1 — Pair via the Nanoleaf mobile app first

> **Required before anything else.** The Nanoleaf Desktop app and the Python bridge both require the device to be on a Thread network. Only the Nanoleaf iOS/Android app can provision the device onto Thread.

1. Factory reset the strip (hold reset ~10s until light flashes)
2. Within 15 minutes of power-on, open the **Nanoleaf mobile app** on iOS or Android → Add Device → follow the Bluetooth pairing flow
3. The mobile app handles Apple Home and Thread provisioning in one flow — **do not add to Apple Home separately first** (it will show as already paired in the Nanoleaf app if you do)
4. Once the mobile app shows the device as connected, the Desktop app and bridge will be able to reach it

> **After any factory reset**, the device gets a new Thread IPv6 address and token — you must repeat this step and update `config_local.py` (see Step 2).

### Step 2 — Get your device info

> **First:** Disable SignalRGB before doing this — it spams the loopback adapter and will pollute the capture.

1. Open Wireshark → select **Npcap Loopback Adapter**
2. Start capture, then apply the display filter:
   ```
   tcp.port == 15765 and http
   ```
3. Open (or reopen) the Nanoleaf Desktop app — it will poll the device immediately on launch
4. Look for a `POST /essentials/state` request in the packet list
5. Right-click it → **Follow → TCP Stream** — this shows the full request/response
6. In the request JSON body (the `devices` array), find your device and grab:
   - `"ip"` — Thread IPv6 address
   - `"token"` — auth token

Example of what you're looking for:
```json
{"devices":[{"controlVersion":2,"defaultName":"Secretlab MAGRGB XXBJ",
  "id":"NXXXXXXXXX","ip":"fdXX:XXXX:XXXX:0:XXXX:XXXX:XXXX:XXXX",
  "model":"NL62","port":5683,"token":"XXXXXXXXXXXXXXXX","eui64":"XXXXXXXXXXXXXXXX"}]}
```

Alternatively — if the device has not been factory reset since your last session, the values in `config_local.py` are still valid and you can skip this step.

### Step 3 — Update config_local.py

```python
DEVICE_MAC = "XX:XX:XX:XX:XX:XX"   # BLE MAC (only needed for HAP bridge)

DEVICE_INFO = {
    "controlVersion": 2,
    "defaultName": "Secretlab MAGRGB XXBJ",
    "id": "NXXXXXXXXX",
    "ip": "fdXX:XXXX:XXXX:0:XXXX:XXXX:XXXX:XXXX",   # Thread IPv6
    "model": "NL62",
    "port": 5683,
    "token": "XXXXXXXXXXXXXXXX",
    "eui64": "XXXXXXXXXXXXXXXX",
}
```

> **After a factory reset** both `ip` and `token` change. Get the new values from the Wireshark capture in Step 2.

### Step 4 — Verify connectivity

```bash
ping -6 <Thread IPv6 address>
```

Should reply via the Apple TV border router at ~1–30ms.

### Step 5 — Run the bridge

Open Nanoleaf Desktop app first (only needed on first launch), then:

```bash
python magnus_wled_bridge_thread.py
```

Output:
```
WLED UDP on 127.0.0.2:21325
WLED HTTP on :80
Enabling stream control via Nanoleaf Desktop...
  OK: b''
Streaming to [fdc5:...]:60222
Ready.
```

After the first successful run the device holds streaming mode — you can close the Nanoleaf Desktop app and it will continue working until the device is power-cycled.

---

## Initial Setup — HAP-BLE (fallback)

Use this if the Thread path isn't available (no Apple TV, or device not on Thread).

### Requirements

- Python 3.9+
- Windows 10/11 with **Bluetooth LE adapter**
- Device factory reset (hold reset button ~10s until light flashes)

### Step 1 — Find the HAP MAC

```bash
python scan.py
python scan_adv.py
```

Look for `Secretlab MAGRGB XXBJ`. The HAP address has `Manufacturer: {76: '06...'}` in the advertisement data.

Set it in `config_local.py`:

```python
DEVICE_MAC = "XX:XX:XX:XX:XX:XX"   # Apple manufacturer ID address
```

### Step 2 — Pair (one-time per device reset)

```bash
python pair.py XXX-XX-XXX
```

Pass the 8-digit HomeKit setup code from the device label. This creates `pairing.json` — never commit it.

> **After any factory reset**, the existing `pairing.json` is invalid and must be regenerated by re-running `pair.py`.

### Step 3 — Test

```bash
python test.py
```

Strip should cycle: RED → GREEN → BLUE → WHITE 50% → OFF.

### Step 4 — Run the bridge

```bash
python magnus_wled_bridge_hapble.py
```

---

## Script Reference

| Script | Purpose | Usage |
|---|---|---|
| `magnus_wled_bridge_thread.py` | **Recommended bridge** — Thread/UDP, ~20 Hz | `python magnus_wled_bridge_thread.py` |
| `magnus_wled_bridge_hapble.py` | Fallback bridge — HAP-BLE, ~10 Hz, needs pairing | `python magnus_wled_bridge_hapble.py` |
| `pair.py` | One-time HAP-BLE pairing, saves `pairing.json` | `python pair.py XXX-XX-XXX` |
| `test.py` | HAP-BLE color cycle test — RED/GREEN/BLUE/WHITE/OFF | `python test.py` |
| `scan.py` | List all nearby BLE devices | `python scan.py` |
| `scan_adv.py` | Show raw advertisement data for MAGRGB addresses | `python scan_adv.py` |
| `discover_services.py` | Enumerate GATT services and characteristics | `python discover_services.py` |

---

## SignalRGB Integration

The strip is exposed to SignalRGB as a **WLED device** — no custom plugin needed.

### Setup

**Step 1 — Start the bridge (run as Administrator for port 80)**

Thread/UDP (recommended):
```bash
python magnus_wled_bridge_thread.py
```

HAP-BLE (fallback, needs pairing first):
```bash
python magnus_wled_bridge_hapble.py
```

**Step 2 — Add in SignalRGB**

1. Open SignalRGB → **Home → Lighting Services → WLED**
2. In "Discover WLED device by IP" enter `127.0.0.2` and press Enter
3. SignalRGB calls `/json/info` on port 80, gets back `"brand": "WLED"` and adds the device
4. Click **Link** — **"Magnus RGB Strip"** is now on your canvas

> **Note:** If you also run the Manka boom arm bridge, it runs on `127.0.0.1:80`. The Magnus bridge runs on `127.0.0.2:80` — different loopback IPs, same port. SignalRGB discovers each by IP with no port suffix needed.

**Step 3 — Assign an effect**

Drag the Magnus RGB Strip block on your canvas and assign any effect.

### Windows Auto-Start

1. Open **Task Scheduler** → **Create Task**
2. **General:** Name `Magnus RGB Bridge`, check **Run with highest privileges**
3. **Triggers:** At startup, delay 30 seconds
4. **Actions:** Start `python`, arguments `C:\path\to\MAGRGB-controller\magnus_wled_bridge_thread.py`, start in `C:\path\to\MAGRGB-controller`
5. Save

> For the Thread bridge, the Nanoleaf Desktop app must have been run at least once after the last device power-cycle to enable streaming mode. After that, the bridge works without the app.

---

## Architecture

### Thread/UDP (recommended)

```
╔══════════════════════════════════════════════════════════════════╗
║                        YOUR PC (127.0.0.2)                       ║
║                                                                  ║
║  ┌──────────┐   ┌───────────────────────────────────────────┐   ║
║  │SignalRGB │   │  magnus_wled_bridge_thread.py             │   ║
║  │          │──▶│  HTTP :80 (WLED discovery)                │   ║
║  │          │   │  UDP  :21325 (DRGB color stream)          │   ║
║  └──────────┘   │  averages zones → single RGB              │   ║
║                  │  UDP socket → [Thread IPv6]:60222  ~20 Hz│   ║
║                  └───────────────────┬───────────────────────┘   ║
╚══════════════════════════════════════╪═══════════════════════════╝
                                       │ IPv6/UDP :60222
                               ┌───────▼────────┐
                               │  Apple TV      │
                               │  Thread Border │
                               │  Router        │
                               └───────┬────────┘
                                       │ Thread radio
                               ┌───────▼────────┐
                               │  Secretlab     │
                               │  MAGRGB (NL62) │
                               └────────────────┘
```

### HAP-BLE (fallback)

```
╔══════════════════════════════════════════════════════════════════╗
║                        YOUR PC (127.0.0.2)                       ║
║                                                                  ║
║  ┌──────────┐   ┌───────────────────────────────────────────┐   ║
║  │SignalRGB │   │  magnus_wled_bridge_hapble.py             │   ║
║  │          │──▶│  HTTP :80 / UDP :21325                    │   ║
║  └──────────┘   │  RGB→HSV conversion                       │   ║
║                  │  aiohomekit put_characteristics  ~10 Hz  │   ║
║                  └───────────────────┬───────────────────────┘   ║
╚══════════════════════════════════════╪═══════════════════════════╝
                                       │ BLE (ChaCha20-Poly1305)
                               ┌───────▼────────┐
                               │  Secretlab     │
                               │  MAGRGB        │
                               │  HAP-BLE GATT  │
                               └────────────────┘
```

---

## Files in This Repo

| File | Purpose |
|---|---|
| `magnus_wled_bridge_thread.py` | **Recommended bridge** — Thread/UDP to device, ~20 Hz, no BLE needed |
| `magnus_wled_bridge_hapble.py` | **Fallback bridge** — HAP-BLE via aiohomekit, ~10 Hz, needs pairing |
| `pair.py` | One-time HAP-BLE SRP pairing, saves `pairing.json` |
| `test.py` | HAP-BLE color cycle test — RED/GREEN/BLUE/WHITE/OFF |
| `scan.py` | BLE scanner — lists all nearby devices with names and MACs |
| `scan_adv.py` | Advertisement scanner — shows raw manufacturer data for MAGRGB addresses |
| `discover_services.py` | GATT service enumerator — lists all services and characteristics |
| `compat.py` | Bleak 2.x compatibility shim used by HAP-BLE scripts |
| `config.py` | Config template — copy to `config_local.py` |
| `config_local.py` | Your device config (MAC + Thread DEVICE_INFO) — gitignored |
| `requirements.txt` | Python dependencies |
| `pairing.json` | Your long-term HAP keypair — generated by `pair.py`, gitignored |

---

## Future Work

- [x] Thread/UDP streaming — 20 Hz single-color via Nanoleaf external control protocol
- [x] HAP-BLE single-color control via lightbulb characteristics (IID 51–54)
- [ ] Per-zone color control — device exposes only panelID=0 in the Thread/Nanoleaf ecosystem; HAP-BLE STRIPES (IID 60) supports zones but produces animated (not static) colors, and device ignores static writes
- [ ] Direct CoAP streaming to device IPv6:5683 — bypasses Nanoleaf Desktop app, eliminates the "run app once" startup requirement
- [ ] Auto-detect HAP MAC address on startup (handles address rotation after reset)

---

## Troubleshooting

### Bridge streams but no lights — after factory reset

**Symptom:** Bridge prints `OK: b'{"...":{"isSuccess":true}}'` and sends `Thread -> rgb(...)` lines, but the strip does not light up.

**Cause:** Factory reset assigns a new Thread IPv6 address and token. `config_local.py` still has the old address, so UDP packets go nowhere.

**Fix:**

1. Pair via the **Nanoleaf mobile app** first (see [Initial Setup — Thread/UDP](#initial-setup--threadudp-recommended) Step 1). The Desktop app cannot provision a Thread device — only the mobile app can.

2. Get the new address from Wireshark. Capture on the loopback adapter, filter:
   ```
   tcp.port == 15765 and http
   ```
   Find `POST /essentials/state` — the request JSON contains the current `ip` and `token`.

3. Update `config_local.py`:
   ```python
   DEVICE_INFO = {
       ...
       "ip":    "fdXX:XXXX:XXXX:0:XXXX:XXXX:XXXX:XXXX",
       "token": "XXXXXXXXXXXXXXXX",
       ...
   }
   ```

4. Restart the bridge.

### Nanoleaf Desktop app can't find the device

**Cause:** The NL62 (MAGRGB) is Thread-only. The Desktop app requires:
- A Thread Border Router on your network (Apple TV 4K, HomePod mini, Google Nest Hub 2nd gen, etc.)
- The device already provisioned onto Thread — **only the Nanoleaf mobile app can do this**

**Fix:** Pair via mobile app first, then re-open the Desktop app. The device will appear automatically once it is on the Thread network.

> **If the Desktop app still can't find it:** Disable SignalRGB before trying. SignalRGB continuously polls `127.0.0.2:80` when the bridge isn't running and appears to interfere with Desktop's Thread device discovery on the loopback adapter. Disable SignalRGB (or remove the WLED device from its canvas), reopen the Desktop app, and it should appear immediately.

---

## Legal

This project was developed for personal interoperability use with hardware the author owns. Reverse engineering for interoperability purposes is permitted under DMCA §1201(f) (US) and equivalent provisions in other jurisdictions.

Not affiliated with, endorsed by, or connected to Secretlab, Nanoleaf, or Apple. All trademarks are property of their respective owners.

Use at your own risk.

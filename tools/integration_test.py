#!/usr/bin/env python3
"""
Integration test for Zigbee DMX Bridge.

Verifies end-to-end that light states set via the Philips Hue REST API
are correctly reflected in the ArtNet DMX output from the ESP32-C6 device.

Test flow:
  1. Connect to the Hue Bridge (reuses .hue_api_key from hue_debug.py)
  2. Identify which Hue light ID(s) correspond to our ESP32 device
  3. Query the ESP32 web API for DMX channel mapping
  4. Set a known color/brightness on the Hue light
  5. Listen for ArtNet packets from the ESP32 on UDP port 6454
  6. Parse the ArtNet DMX512 data and verify the expected channel values

ArtNet protocol overview:
  - UDP port 6454
  - OpDmx (0x5000): 18-byte header + up to 512 DMX channel bytes
  - Header: "Art-Net\0" (8 bytes) + opcode LE (2) + proto ver BE (2)
            + sequence (1) + physical (1) + universe LE (2) + length BE (2)

Prerequisites:
  pip install requests

Usage:
  # Auto-discover bridge, identify our device's light(s), run tests:
  python3 tools/integration_test.py --device-ip 192.168.178.110

  # Specify bridge IP and light ID explicitly:
  python3 tools/integration_test.py --bridge-ip 192.168.178.216 --light-id 25

  # Specify ArtNet listen interface (default: 0.0.0.0):
  python3 tools/integration_test.py --device-ip 192.168.178.110 --listen-ip 0.0.0.0

  # Skip ArtNet verification (only test Hue API round-trip):
  python3 tools/integration_test.py --device-ip 192.168.178.110 --skip-artnet

  # Increase wait time for slow Zigbee propagation:
  python3 tools/integration_test.py --device-ip 192.168.178.110 --settle-time 3.0
"""

import argparse
import colorsys
import json
import math
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("ERROR: 'requests' package is required. Install with: pip install requests",
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

ARTNET_PORT = 6454
ARTNET_HEADER = b"Art-Net\x00"
ARTNET_OP_DMX = 0x5000
ARTNET_PROTOCOL_VERSION = 14

# Hue API key file (shared with hue_debug.py)
API_KEY_FILE = ".hue_api_key"

# Our device's manufacturer name as registered in Zigbee basic cluster
DEVICE_MANUFACTURER = "ZigbeeDMX"

# Tolerance for DMX value comparison (to account for float rounding)
DMX_TOLERANCE = 2

# Default settle time after sending a Hue command (seconds)
DEFAULT_SETTLE_TIME = 2.0


# ---------------------------------------------------------------------------
#  ArtNet Listener
# ---------------------------------------------------------------------------

class ArtNetListener:
    """Listens for ArtNet DMX packets on a UDP socket."""

    def __init__(self, listen_ip: str = "0.0.0.0", universe: int = 0):
        self.listen_ip = listen_ip
        self.universe = universe
        self.sock = None
        self.running = False
        self.thread = None
        self.last_packet = None       # raw bytes of last matching OpDmx
        self.last_dmx_data = None     # bytearray of DMX channel values (1-indexed concept, 0-indexed array)
        self.last_source_ip = None
        self.packet_count = 0
        self._lock = threading.Lock()

    def start(self):
        """Start listening in a background thread."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Allow receiving broadcast packets
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
        self.sock.bind((self.listen_ip, ARTNET_PORT))
        self.sock.settimeout(1.0)
        self.running = True
        self.thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.thread.start()
        print(f"  ArtNet listener started on {self.listen_ip}:{ARTNET_PORT} "
              f"(universe {self.universe})")

    def stop(self):
        """Stop the listener."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)
        if self.sock:
            self.sock.close()
            self.sock = None

    def _listen_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(1024)
                self._handle_packet(data, addr)
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    raise
                break

    def _handle_packet(self, data: bytes, addr: tuple):
        """Parse an ArtNet packet."""
        if len(data) < 18:
            return
        # Check Art-Net header
        if data[:8] != ARTNET_HEADER:
            return
        # Opcode (little-endian)
        opcode = struct.unpack_from("<H", data, 8)[0]
        if opcode != ARTNET_OP_DMX:
            return
        # Protocol version (big-endian)
        proto_ver = struct.unpack_from(">H", data, 10)[0]
        if proto_ver < ARTNET_PROTOCOL_VERSION:
            return
        # Sequence, physical, universe (LE), length (BE)
        sequence = data[12]
        physical = data[13]
        universe = struct.unpack_from("<H", data, 14)[0]
        dmx_length = struct.unpack_from(">H", data, 16)[0]

        if universe != self.universe:
            return

        # Extract DMX data
        dmx_data = bytearray(data[18:18 + dmx_length])

        with self._lock:
            self.last_packet = data
            self.last_dmx_data = dmx_data
            self.last_source_ip = addr[0]
            self.packet_count += 1

    def get_dmx_data(self) -> Optional[bytearray]:
        """Return the latest DMX data snapshot, or None if no packet received."""
        with self._lock:
            if self.last_dmx_data is not None:
                return bytearray(self.last_dmx_data)
        return None

    def get_channel(self, channel: int) -> Optional[int]:
        """Get a single DMX channel value (1-indexed). Returns None if no data."""
        with self._lock:
            if self.last_dmx_data is None:
                return None
            idx = channel - 1  # DMX channels are 1-indexed, array is 0-indexed
            if idx < 0 or idx >= len(self.last_dmx_data):
                return None
            return self.last_dmx_data[idx]

    def wait_for_packet(self, timeout: float = 5.0) -> bool:
        """Wait until at least one packet is received. Returns True if received."""
        start = time.time()
        initial_count = self.packet_count
        while time.time() - start < timeout:
            if self.packet_count > initial_count:
                return True
            time.sleep(0.05)
        return False

    def clear(self):
        """Clear the last received data (useful before a new test)."""
        with self._lock:
            self.last_dmx_data = None
            self.last_packet = None
            self.packet_count = 0


# ---------------------------------------------------------------------------
#  Hue API helpers (reused from hue_debug.py)
# ---------------------------------------------------------------------------

def get_api_key_path() -> Path:
    """Return the path to the API key file (in project root)."""
    script_dir = Path(__file__).resolve().parent.parent
    pio_ini = script_dir / "platformio.ini"
    if pio_ini.exists():
        return script_dir / API_KEY_FILE
    return Path.cwd() / API_KEY_FILE


def load_api_key() -> Optional[str]:
    """Load persisted API key from disk."""
    key_path = get_api_key_path()
    if key_path.exists():
        key = key_path.read_text().strip()
        if key:
            return key
    return None


def discover_bridge() -> Optional[str]:
    """Discover Hue Bridge IP via NUPNP."""
    try:
        r = requests.get("https://discovery.meethue.com", timeout=10)
        r.raise_for_status()
        bridges = r.json()
        if bridges:
            return bridges[0].get("internalipaddress")
    except Exception as e:
        print(f"  Bridge discovery failed: {e}")
    return None


class HueAPI:
    """Minimal Hue Bridge REST API client."""

    def __init__(self, bridge_ip: str, api_key: str):
        self.bridge_ip = bridge_ip
        self.base_url = f"https://{bridge_ip}/api/{api_key}"

    def _get(self, path: str) -> dict:
        r = requests.get(f"{self.base_url}{path}", verify=False, timeout=10)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, data: dict) -> list:
        r = requests.put(f"{self.base_url}{path}", json=data,
                         verify=False, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_lights(self) -> dict:
        return self._get("/lights")

    def get_light(self, light_id: str) -> dict:
        return self._get(f"/lights/{light_id}")

    def set_light_state(self, light_id: str, state: dict) -> list:
        return self._put(f"/lights/{light_id}/state", state)


# ---------------------------------------------------------------------------
#  Device API helpers
# ---------------------------------------------------------------------------

def get_device_config(device_ip: str) -> Optional[dict]:
    """Query the ESP32 device config API."""
    try:
        r = requests.get(f"http://{device_ip}/api/config", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Failed to get device config: {e}")
        return None


def get_device_status(device_ip: str) -> Optional[dict]:
    """Query the ESP32 device status API."""
    try:
        r = requests.get(f"http://{device_ip}/api/status", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Failed to get device status: {e}")
        return None


# ---------------------------------------------------------------------------
#  Color conversion helpers (must match firmware's zigbee_manager.cpp)
# ---------------------------------------------------------------------------

def cie_xy_to_rgb(x: float, y: float) -> tuple:
    """
    Convert CIE 1931 xy chromaticity to RGB (0-255).
    Matches the firmware's xyToRGB() function.
    """
    if y < 0.001:
        y = 0.001
    Y = 1.0
    X = (Y / y) * x
    Z = (Y / y) * (1.0 - x - y)

    # Wide-gamut D65 matrix (sRGB)
    rf = X * 3.2404542 + Y * -1.5371385 + Z * -0.4985314
    gf = X * -0.9692660 + Y * 1.8760108 + Z * 0.0415560
    bf = X * 0.0556434 + Y * -0.2040259 + Z * 1.0572252

    # Clamp
    rf = max(0.0, min(1.0, rf))
    gf = max(0.0, min(1.0, gf))
    bf = max(0.0, min(1.0, bf))

    # Reverse gamma (sRGB)
    def reverse_gamma(v):
        return 12.92 * v if v <= 0.0031308 else 1.055 * (v ** (1.0 / 2.4)) - 0.055

    rf = reverse_gamma(rf)
    gf = reverse_gamma(gf)
    bf = reverse_gamma(bf)

    # Scale so max component = 255
    max_c = max(rf, gf, bf)
    if max_c > 0.001:
        scale = 255.0 / max_c
        r = int(rf * scale + 0.5)
        g = int(gf * scale + 0.5)
        b = int(bf * scale + 0.5)
    else:
        r = g = b = 0

    return (r, g, b)


def rgb_to_cie_xy(r: int, g: int, b: int) -> tuple:
    """
    Convert RGB (0-255) to CIE 1931 xy.
    Matches the firmware's rgbToXY().
    """
    def apply_gamma(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    rf = apply_gamma(r)
    gf = apply_gamma(g)
    bf = apply_gamma(b)

    X = rf * 0.4124564 + gf * 0.3575761 + bf * 0.1804375
    Y = rf * 0.2126729 + gf * 0.7151522 + bf * 0.0721750
    Z = rf * 0.0193339 + gf * 0.1191920 + bf * 0.9503041

    total = X + Y + Z
    if total < 0.001:
        return (0.3127, 0.3290)
    return (X / total, Y / total)


def hue_xy_to_zcl(x: float, y: float) -> tuple:
    """Convert Hue API xy (0.0-1.0 float) to ZCL xy (0-65279 uint16)."""
    return (int(x * 65279.0 + 0.5), int(y * 65279.0 + 0.5))


def zcl_xy_to_hue(zcl_x: int, zcl_y: int) -> tuple:
    """Convert ZCL xy (0-65279) to Hue API xy (0.0-1.0 float)."""
    return (zcl_x / 65279.0, zcl_y / 65279.0)


# ---------------------------------------------------------------------------
#  Test helpers
# ---------------------------------------------------------------------------

def find_device_lights(api: HueAPI, device_eui64: Optional[str] = None,
                       manufacturer: str = DEVICE_MANUFACTURER) -> dict:
    """
    Find Hue light IDs belonging to our device.
    Returns {hue_light_id: light_data} for matching lights.
    """
    all_lights = api.get_lights()
    matches = {}
    for lid, light in all_lights.items():
        mfr = light.get("manufacturername", "")
        uid = light.get("uniqueid", "")
        if mfr == manufacturer:
            matches[lid] = light
        elif device_eui64 and device_eui64.lower().replace(":", "") in uid.lower().replace(":", "").replace("-", ""):
            matches[lid] = light
    return matches


def compute_expected_dmx(r: int, g: int, b: int, brightness: int,
                         power_on: bool) -> tuple:
    """
    Compute the expected DMX channel values after brightness scaling.
    Matches firmware's dmx_output.cpp update() logic.

    brightness: Zigbee scale 0-254
    Returns (dmx_r, dmx_g, dmx_b)
    """
    if not power_on:
        return (0, 0, 0)
    bri_scale = brightness / 254.0
    dmx_r = int(r * bri_scale)
    dmx_g = int(g * bri_scale)
    dmx_b = int(b * bri_scale)
    return (dmx_r, dmx_g, dmx_b)


class TestResult:
    """Collects pass/fail results."""

    def __init__(self):
        self.tests = []
        self.passed = 0
        self.failed = 0

    def check(self, name: str, expected, actual, tolerance=DMX_TOLERANCE):
        """Check a single value with tolerance."""
        if isinstance(expected, tuple) and isinstance(actual, tuple):
            ok = all(abs(e - a) <= tolerance for e, a in zip(expected, actual))
        elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            ok = abs(expected - actual) <= tolerance
        else:
            ok = (expected == actual)

        status = "PASS" if ok else "FAIL"
        self.tests.append((name, status, expected, actual))
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        return ok

    def summary(self):
        print()
        print("=" * 60)
        print(f"  TEST RESULTS: {self.passed} passed, {self.failed} failed, "
              f"{self.passed + self.failed} total")
        print("=" * 60)
        for name, status, expected, actual in self.tests:
            icon = "PASS" if status == "PASS" else "FAIL"
            print(f"  [{icon}] {name}")
            if status == "FAIL":
                print(f"         expected: {expected}")
                print(f"         actual:   {actual}")
        print()
        return self.failed == 0


# ---------------------------------------------------------------------------
#  Test scenarios
# ---------------------------------------------------------------------------

def test_light_on_off(api: HueAPI, light_id: str, listener: Optional[ArtNetListener],
                      dmx_start: int, channel_map: dict, results: TestResult,
                      settle_time: float):
    """Test turning a light on and off."""
    print(f"\n--- Test: ON/OFF for light #{light_id} ---")

    # Turn ON, full brightness, white
    print("  Setting light ON, brightness=254, white...")
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.3127, 0.3290]})
    time.sleep(settle_time)

    state = api.get_light(light_id).get("state", {})
    results.check(f"Light {light_id} ON: Hue reports on=True",
                  True, state.get("on"))

    if listener:
        dmx = listener.get_dmx_data()
        if dmx is None:
            results.check(f"Light {light_id} ON: ArtNet packet received", True, False)
        else:
            # White should produce roughly equal RGB at full brightness
            r_ch = dmx_start + channel_map.get("r", 0) - 1  # 0-indexed
            g_ch = dmx_start + channel_map.get("g", 1) - 1
            b_ch = dmx_start + channel_map.get("b", 2) - 1
            dmx_r = dmx[r_ch] if r_ch < len(dmx) else 0
            dmx_g = dmx[g_ch] if g_ch < len(dmx) else 0
            dmx_b = dmx[b_ch] if b_ch < len(dmx) else 0
            print(f"  ArtNet DMX values: R={dmx_r} G={dmx_g} B={dmx_b}")
            # White at full brightness should have high values on all channels
            results.check(f"Light {light_id} ON: DMX R > 200", True, dmx_r > 200)
            results.check(f"Light {light_id} ON: DMX G > 200", True, dmx_g > 200)
            results.check(f"Light {light_id} ON: DMX B > 200", True, dmx_b > 200)

    # Turn OFF
    print("  Setting light OFF...")
    api.set_light_state(light_id, {"on": False})
    time.sleep(settle_time)

    state = api.get_light(light_id).get("state", {})
    results.check(f"Light {light_id} OFF: Hue reports on=False",
                  False, state.get("on"))

    if listener:
        dmx = listener.get_dmx_data()
        if dmx is not None:
            r_ch = dmx_start + channel_map.get("r", 0) - 1
            g_ch = dmx_start + channel_map.get("g", 1) - 1
            b_ch = dmx_start + channel_map.get("b", 2) - 1
            dmx_r = dmx[r_ch] if r_ch < len(dmx) else 0
            dmx_g = dmx[g_ch] if g_ch < len(dmx) else 0
            dmx_b = dmx[b_ch] if b_ch < len(dmx) else 0
            print(f"  ArtNet DMX values: R={dmx_r} G={dmx_g} B={dmx_b}")
            results.check(f"Light {light_id} OFF: DMX R == 0", 0, dmx_r)
            results.check(f"Light {light_id} OFF: DMX G == 0", 0, dmx_g)
            results.check(f"Light {light_id} OFF: DMX B == 0", 0, dmx_b)


def test_light_color(api: HueAPI, light_id: str, listener: Optional[ArtNetListener],
                     dmx_start: int, channel_map: dict, results: TestResult,
                     settle_time: float):
    """Test setting specific colors via CIE xy coordinates."""
    print(f"\n--- Test: COLOR for light #{light_id} ---")

    # Test cases: (name, hue_xy, expected_dominant_channel)
    # Using CIE xy coordinates for saturated red, green, blue
    test_colors = [
        ("Red",   [0.6750, 0.3220], "r"),  # Saturated red
        ("Green", [0.4091, 0.5180], "g"),  # Saturated green
        ("Blue",  [0.1670, 0.0400], "b"),  # Saturated blue
    ]

    # Ensure light is on at full brightness
    api.set_light_state(light_id, {"on": True, "bri": 254})
    time.sleep(0.5)

    for color_name, xy, dominant in test_colors:
        print(f"  Setting color: {color_name} (xy={xy})...")
        api.set_light_state(light_id, {"xy": xy})
        time.sleep(settle_time)

        # Verify Hue reports the color
        state = api.get_light(light_id).get("state", {})
        reported_xy = state.get("xy", [0, 0])
        results.check(f"Light {light_id} {color_name}: Hue xy[0] ~= {xy[0]:.3f}",
                      xy[0], reported_xy[0], tolerance=0.05)
        results.check(f"Light {light_id} {color_name}: Hue xy[1] ~= {xy[1]:.3f}",
                      xy[1], reported_xy[1], tolerance=0.05)

        if listener:
            dmx = listener.get_dmx_data()
            if dmx is None:
                results.check(f"Light {light_id} {color_name}: ArtNet received",
                              True, False)
                continue

            r_ch = dmx_start + channel_map.get("r", 0) - 1
            g_ch = dmx_start + channel_map.get("g", 1) - 1
            b_ch = dmx_start + channel_map.get("b", 2) - 1
            dmx_r = dmx[r_ch] if r_ch < len(dmx) else 0
            dmx_g = dmx[g_ch] if g_ch < len(dmx) else 0
            dmx_b = dmx[b_ch] if b_ch < len(dmx) else 0
            print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b}")

            # The dominant channel should be the highest
            channels = {"r": dmx_r, "g": dmx_g, "b": dmx_b}
            max_ch = max(channels, key=lambda k: channels[k])
            results.check(f"Light {light_id} {color_name}: dominant channel is '{dominant}'",
                          dominant, max_ch)

            # The dominant channel should be reasonably high (> 100)
            results.check(f"Light {light_id} {color_name}: dominant value > 100",
                          True, channels[dominant] > 100)


def test_light_brightness(api: HueAPI, light_id: str, listener: Optional[ArtNetListener],
                          dmx_start: int, channel_map: dict, results: TestResult,
                          settle_time: float):
    """Test brightness scaling."""
    print(f"\n--- Test: BRIGHTNESS for light #{light_id} ---")

    # Set to white first
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.3127, 0.3290]})
    time.sleep(settle_time)

    brightness_levels = [254, 127, 1]

    for bri in brightness_levels:
        print(f"  Setting brightness={bri}...")
        api.set_light_state(light_id, {"bri": bri})
        time.sleep(settle_time)

        state = api.get_light(light_id).get("state", {})
        reported_bri = state.get("bri", 0)
        results.check(f"Light {light_id} bri={bri}: Hue reports bri={bri}",
                      bri, reported_bri, tolerance=1)

        if listener:
            dmx = listener.get_dmx_data()
            if dmx is None:
                results.check(f"Light {light_id} bri={bri}: ArtNet received",
                              True, False)
                continue

            r_ch = dmx_start + channel_map.get("r", 0) - 1
            g_ch = dmx_start + channel_map.get("g", 1) - 1
            b_ch = dmx_start + channel_map.get("b", 2) - 1
            dmx_r = dmx[r_ch] if r_ch < len(dmx) else 0
            dmx_g = dmx[g_ch] if g_ch < len(dmx) else 0
            dmx_b = dmx[b_ch] if b_ch < len(dmx) else 0
            print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b}")

            # For white, all channels should be roughly equal
            # and scaled by brightness
            # Firmware: dmx_val = rgb_val * (bri / 254.0)
            # For white at full brightness, RGB from xyToRGB(0.3127, 0.3290)
            # should be roughly (255, 255, 255), so:
            expected_approx = int(255 * (bri / 254.0))
            results.check(f"Light {light_id} bri={bri}: DMX R ~= {expected_approx}",
                          expected_approx, dmx_r, tolerance=15)
            results.check(f"Light {light_id} bri={bri}: DMX G ~= {expected_approx}",
                          expected_approx, dmx_g, tolerance=15)


def test_light_color_accuracy(api: HueAPI, light_id: str,
                               listener: Optional[ArtNetListener],
                               dmx_start: int, channel_map: dict,
                               results: TestResult, settle_time: float):
    """
    Test that the CIE XY -> RGB -> DMX pipeline produces accurate values.
    Uses known xy coordinates and computes expected RGB using the same
    algorithm as the firmware.
    """
    print(f"\n--- Test: COLOR ACCURACY for light #{light_id} ---")

    api.set_light_state(light_id, {"on": True, "bri": 254})
    time.sleep(0.5)

    # Test cases: (name, xy, expected_rgb_from_firmware_algo)
    test_cases = [
        ("White D65",   [0.3127, 0.3290]),
        ("Red",         [0.6750, 0.3220]),
        ("Green",       [0.4091, 0.5180]),
        ("Blue",        [0.1670, 0.0400]),
        ("Warm White",  [0.4578, 0.4101]),
        ("Magenta",     [0.3833, 0.1591]),
    ]

    for name, xy in test_cases:
        print(f"  Testing {name} (xy={xy})...")
        api.set_light_state(light_id, {"xy": xy})
        time.sleep(settle_time)

        if not listener:
            continue

        dmx = listener.get_dmx_data()
        if dmx is None:
            results.check(f"Light {light_id} {name}: ArtNet received", True, False)
            continue

        r_ch = dmx_start + channel_map.get("r", 0) - 1
        g_ch = dmx_start + channel_map.get("g", 1) - 1
        b_ch = dmx_start + channel_map.get("b", 2) - 1
        dmx_r = dmx[r_ch] if r_ch < len(dmx) else 0
        dmx_g = dmx[g_ch] if g_ch < len(dmx) else 0
        dmx_b = dmx[b_ch] if b_ch < len(dmx) else 0

        # Compute expected RGB using our Python port of the firmware's algo
        exp_r, exp_g, exp_b = cie_xy_to_rgb(xy[0], xy[1])
        # At bri=254, scale is 254/254 = 1.0, so DMX = RGB
        bri_scale = 254 / 254.0
        exp_dmx_r = int(exp_r * bri_scale)
        exp_dmx_g = int(exp_g * bri_scale)
        exp_dmx_b = int(exp_b * bri_scale)

        print(f"    Expected DMX: R={exp_dmx_r} G={exp_dmx_g} B={exp_dmx_b}")
        print(f"    Actual   DMX: R={dmx_r} G={dmx_g} B={dmx_b}")

        # Allow some tolerance for float rounding differences
        results.check(f"Light {light_id} {name}: R accuracy",
                      exp_dmx_r, dmx_r, tolerance=5)
        results.check(f"Light {light_id} {name}: G accuracy",
                      exp_dmx_g, dmx_g, tolerance=5)
        results.check(f"Light {light_id} {name}: B accuracy",
                      exp_dmx_b, dmx_b, tolerance=5)


def mirek_to_rgb(mirek: int) -> tuple:
    """
    Convert mirek (color temperature) to RGB.
    Matches the firmware's mirekToRGB() function.
    """
    if mirek < 153:
        mirek = 153
    if mirek > 500:
        mirek = 500

    kelvin = 1000000.0 / mirek
    temp = kelvin / 100.0

    # Red
    if temp <= 66.0:
        rf = 255.0
    else:
        rf = 329.698727446 * ((temp - 60.0) ** -0.1332047592)

    # Green
    if temp <= 66.0:
        gf = 99.4708025861 * math.log(temp) - 161.1195681661
    else:
        gf = 288.1221695283 * ((temp - 60.0) ** -0.0755148492)

    # Blue
    if temp >= 66.0:
        bf = 255.0
    elif temp <= 19.0:
        bf = 0.0
    else:
        bf = 138.5177312231 * math.log(temp - 10.0) - 305.0447927307

    rf = max(0.0, min(255.0, rf))
    gf = max(0.0, min(255.0, gf))
    bf = max(0.0, min(255.0, bf))

    # Normalize so max channel = 255
    max_c = max(rf, gf, bf)
    if max_c > 0.001:
        scale = 255.0 / max_c
        r = int(rf * scale + 0.5)
        g = int(gf * scale + 0.5)
        b = int(bf * scale + 0.5)
    else:
        r = g = b = 255

    return (r, g, b)


def test_light_color_temperature(api: HueAPI, light_id: str,
                                  listener: Optional[ArtNetListener],
                                  dmx_start: int, channel_map: dict,
                                  results: TestResult, settle_time: float):
    """
    Test color temperature (mirek) control via the Hue API 'ct' parameter.
    Verifies that the firmware's mirekToRGB conversion produces expected DMX values.
    """
    print(f"\n--- Test: COLOR TEMPERATURE for light #{light_id} ---")

    api.set_light_state(light_id, {"on": True, "bri": 254})
    time.sleep(0.5)

    # Test cases: (name, mirek_value, description)
    test_cases = [
        ("Cool Daylight", 153, "6536K - bluish white"),
        ("Daylight",      250, "4000K - neutral white"),
        ("Warm White",    370, "2703K - warm white"),
        ("Candle",        500, "2000K - very warm/orange"),
    ]

    for name, mirek, desc in test_cases:
        print(f"  Setting CT={mirek} mirek ({desc})...")
        response = api.set_light_state(light_id, {"ct": mirek})
        time.sleep(settle_time)

        # Verify Hue reports the color temperature
        state = api.get_light(light_id).get("state", {})
        reported_ct = state.get("ct", 0)
        results.check(f"Light {light_id} CT {name}: Hue reports ct ~= {mirek}",
                      mirek, reported_ct, tolerance=5)

        if not listener:
            continue

        dmx = listener.get_dmx_data()
        if dmx is None:
            results.check(f"Light {light_id} CT {name}: ArtNet received", True, False)
            continue

        r_ch = dmx_start + channel_map.get("r", 0) - 1
        g_ch = dmx_start + channel_map.get("g", 1) - 1
        b_ch = dmx_start + channel_map.get("b", 2) - 1
        dmx_r = dmx[r_ch] if r_ch < len(dmx) else 0
        dmx_g = dmx[g_ch] if g_ch < len(dmx) else 0
        dmx_b = dmx[b_ch] if b_ch < len(dmx) else 0

        # Compute expected RGB from our Python port of the firmware
        exp_r, exp_g, exp_b = mirek_to_rgb(mirek)

        print(f"    Expected DMX: R={exp_r} G={exp_g} B={exp_b}")
        print(f"    Actual   DMX: R={dmx_r} G={dmx_g} B={dmx_b}")

        results.check(f"Light {light_id} CT {name}: R accuracy",
                      exp_r, dmx_r, tolerance=10)
        results.check(f"Light {light_id} CT {name}: G accuracy",
                      exp_g, dmx_g, tolerance=10)
        results.check(f"Light {light_id} CT {name}: B accuracy",
                      exp_b, dmx_b, tolerance=10)

        # Verify warm is more red, cool is more blue
        if mirek >= 370:
            results.check(f"Light {light_id} CT {name}: warm has R > B",
                          True, dmx_r >= dmx_b)
        elif mirek <= 200:
            results.check(f"Light {light_id} CT {name}: cool has B > 0",
                          True, dmx_b > 50)


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Integration test: verify Hue API light states match ArtNet DMX output",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--bridge-ip",
                        help="Hue Bridge IP (auto-discovered if omitted)")
    parser.add_argument("--api-key",
                        help="Hue API key (loaded from .hue_api_key if omitted)")
    parser.add_argument("--device-ip",
                        help="ESP32 Zigbee DMX Bridge IP for config/status queries")
    parser.add_argument("--light-id",
                        help="Hue light ID to test (auto-detected if --device-ip given)")
    parser.add_argument("--listen-ip", default="0.0.0.0",
                        help="IP to bind ArtNet listener (default: 0.0.0.0)")
    parser.add_argument("--universe", type=int, default=0,
                        help="ArtNet universe to listen on (default: 0)")
    parser.add_argument("--skip-artnet", action="store_true",
                        help="Skip ArtNet verification, only test Hue API round-trip")
    parser.add_argument("--settle-time", type=float, default=DEFAULT_SETTLE_TIME,
                        help=f"Seconds to wait after Hue command (default: {DEFAULT_SETTLE_TIME})")
    parser.add_argument("--test", choices=["all", "onoff", "color", "brightness", "accuracy", "colortemp"],
                        default="all",
                        help="Which test to run (default: all)")

    args = parser.parse_args()

    # --- Discover bridge ---
    bridge_ip = args.bridge_ip
    if not bridge_ip:
        bridge_ip = discover_bridge()
        if not bridge_ip:
            print("ERROR: Could not discover Hue Bridge. Use --bridge-ip.",
                  file=sys.stderr)
            sys.exit(1)
    print(f"Hue Bridge: {bridge_ip}")

    # --- API key ---
    api_key = args.api_key or load_api_key()
    if not api_key:
        print("ERROR: No Hue API key found. Run tools/hue_debug.py first "
              "to create one.", file=sys.stderr)
        sys.exit(1)
    print(f"API key: {api_key[:8]}...")

    api = HueAPI(bridge_ip, api_key)

    # --- Identify our device's lights ---
    device_config = None
    device_eui64 = None
    light_ids = []

    if args.device_ip:
        print(f"\nQuerying device at {args.device_ip}...")
        device_config = get_device_config(args.device_ip)
        device_status = get_device_status(args.device_ip)
        if device_status:
            device_eui64 = device_status.get("eui64")
            print(f"  Device EUI64: {device_eui64}")
            print(f"  Zigbee: {device_status.get('zigbee')}")
        if device_config:
            lights_cfg = device_config.get("lights", [])
            output_cfg = device_config.get("output", {})
            print(f"  Configured lights: {len(lights_cfg)}")
            for i, lc in enumerate(lights_cfg):
                print(f"    [{i}] {lc.get('name')} type={lc.get('type')} "
                      f"dmxAddr={lc.get('dmxAddr')} (endpoint {10 + i})")
            print(f"  Output mode: {output_cfg.get('mode', 'dmx')}")
            if output_cfg.get("mode") == "artnet":
                args.universe = output_cfg.get("artnetUniverse", args.universe)
                print(f"  ArtNet universe: {args.universe}")

    # Find matching Hue lights
    print(f"\nSearching for {DEVICE_MANUFACTURER} lights on bridge...")
    matching = find_device_lights(api, device_eui64)
    if matching:
        print(f"  Found {len(matching)} matching light(s):")
        for lid, light in sorted(matching.items(), key=lambda x: int(x[0])):
            print(f"    Light #{lid}: {light.get('name')} "
                  f"(uniqueid={light.get('uniqueid')})")
        light_ids = sorted(matching.keys(), key=int)
    elif args.light_id:
        light_ids = [args.light_id]
        print(f"  Using explicitly specified light ID: {args.light_id}")
    else:
        print("  ERROR: No matching lights found on bridge.", file=sys.stderr)
        print("  Use --light-id to specify manually, or pair the device first.",
              file=sys.stderr)
        sys.exit(1)

    if args.light_id:
        # Override with explicit light ID
        light_ids = [args.light_id]

    # --- Build DMX mapping from device config ---
    # Maps hue_light_id -> (dmx_start_addr, channel_map)
    dmx_mappings = {}
    if device_config:
        lights_cfg = device_config.get("lights", [])
        # Match by index: light_ids[0] = lights_cfg[0], etc.
        # This assumes the Hue lights are in endpoint order
        for i, lid in enumerate(light_ids):
            if i < len(lights_cfg):
                lc = lights_cfg[i]
                dmx_mappings[lid] = (
                    lc.get("dmxAddr", 1),
                    lc.get("channelMap", {"r": 0, "g": 1, "b": 2, "w": 3})
                )
            else:
                # Default mapping
                dmx_mappings[lid] = (1 + i * 3, {"r": 0, "g": 1, "b": 2})
    else:
        # No device config, assume default
        for i, lid in enumerate(light_ids):
            dmx_mappings[lid] = (1 + i * 3, {"r": 0, "g": 1, "b": 2})

    # --- Start ArtNet listener ---
    listener = None
    if not args.skip_artnet:
        print(f"\nStarting ArtNet listener (universe={args.universe})...")
        listener = ArtNetListener(listen_ip=args.listen_ip, universe=args.universe)
        try:
            listener.start()
        except OSError as e:
            print(f"  WARNING: Could not start ArtNet listener: {e}")
            print(f"  (Is another process using port {ARTNET_PORT}?)")
            print(f"  Continuing without ArtNet verification.")
            listener = None

        if listener:
            # Wait for initial packet
            print("  Waiting for initial ArtNet packet...")
            if listener.wait_for_packet(timeout=10):
                print(f"  Received ArtNet from {listener.last_source_ip}")
            else:
                print("  WARNING: No ArtNet packets received within 10s.")
                print("  Is the device configured for ArtNet output?")
                print("  Continuing tests (ArtNet checks will fail).")

    # --- Run tests ---
    results = TestResult()

    for lid in light_ids:
        dmx_start, channel_map = dmx_mappings.get(lid, (1, {"r": 0, "g": 1, "b": 2}))
        print(f"\n{'=' * 60}")
        print(f"  TESTING LIGHT #{lid} (DMX start={dmx_start}, map={channel_map})")
        print(f"{'=' * 60}")

        tests_to_run = args.test
        if tests_to_run in ("all", "onoff"):
            test_light_on_off(api, lid, listener, dmx_start, channel_map,
                              results, args.settle_time)
        if tests_to_run in ("all", "color"):
            test_light_color(api, lid, listener, dmx_start, channel_map,
                             results, args.settle_time)
        if tests_to_run in ("all", "brightness"):
            test_light_brightness(api, lid, listener, dmx_start, channel_map,
                                  results, args.settle_time)
        if tests_to_run in ("all", "accuracy"):
            test_light_color_accuracy(api, lid, listener, dmx_start, channel_map,
                                       results, args.settle_time)
        if tests_to_run in ("all", "colortemp"):
            test_light_color_temperature(api, lid, listener, dmx_start, channel_map,
                                          results, args.settle_time)

        # Restore light to a neutral state
        print(f"\n  Restoring light #{lid} to white, ON, bri=254...")
        api.set_light_state(lid, {"on": True, "bri": 254, "xy": [0.3127, 0.3290]})

    # --- Cleanup ---
    if listener:
        listener.stop()

    # --- Report ---
    all_passed = results.summary()
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()

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

Tests include:
  - ON/OFF: verify power state is reflected in DMX output
  - COLOR: verify CIE xy color changes produce correct DMX RGB values
  - BRIGHTNESS: verify brightness scaling in DMX output
  - ACCURACY: verify device-reported state matches ArtNet output
  - COLORTEMP: verify mirek (color temperature) control
  - RGBW: verify white channel decomposition for RGBW-configured lights
    (W = min(R,G,B), then R'=R-W, G'=G-W, B'=B-W)

ArtNet protocol overview:
  - UDP port 6454
  - OpDmx (0x5000): 18-byte header + up to 512 DMX channel bytes
  - Header: "Art-Net\\0" (8 bytes) + opcode LE (2) + proto ver BE (2)
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

  # Run only RGBW tests:
  python3 tools/integration_test.py --device-ip 192.168.178.110 --test rgbw
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

    def wait_for_fresh_data(self, timeout: float = 5.0) -> Optional[bytearray]:
        """Clear old data, wait for a new packet, and return the DMX data."""
        with self._lock:
            self.last_dmx_data = None
            self.last_packet = None
            initial_count = self.packet_count
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                if self.packet_count > initial_count and self.last_dmx_data is not None:
                    return bytearray(self.last_dmx_data)
            time.sleep(0.05)
        return None

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
        result = r.json()
        # Check for Hue API errors in the response
        if isinstance(result, list):
            for item in result:
                if "error" in item:
                    desc = item["error"].get("description", "unknown error")
                    print(f"    WARNING: Hue API error: {desc}")
        return result

    def get_lights(self) -> dict:
        return self._get("/lights")

    def get_light(self, light_id: str) -> dict:
        return self._get(f"/lights/{light_id}")

    def set_light_state(self, light_id: str, state: dict) -> list:
        return self._put(f"/lights/{light_id}/state", state)


# ---------------------------------------------------------------------------
#  Device API helpers
# ---------------------------------------------------------------------------

# The ESP32-C6 WiFi stack is severely impacted by 802.15.4 coexistence,
# causing high latency (1-2s) and 30-50% packet loss.  We need generous
# timeouts and automatic retries for every HTTP call to the device.

DEVICE_TIMEOUT = 20      # seconds per attempt
DEVICE_RETRIES = 3       # number of attempts
DEVICE_RETRY_DELAY = 2   # seconds between retries


def _device_request(method: str, url: str, retries: int = DEVICE_RETRIES,
                    **kwargs) -> Optional[requests.Response]:
    """Make an HTTP request to the device with retries."""
    kwargs.setdefault("timeout", DEVICE_TIMEOUT)
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt < retries:
                print(f"  Retry {attempt}/{retries} for {url}: {e}")
                time.sleep(DEVICE_RETRY_DELAY)
            else:
                print(f"  Failed after {retries} attempts for {url}: {e}")
    return None


def get_device_config(device_ip: str) -> Optional[dict]:
    """Query the ESP32 device config API."""
    r = _device_request("GET", f"http://{device_ip}/api/config")
    if r is not None:
        return r.json()
    return None


def set_device_config(device_ip: str, config: dict) -> bool:
    """Push a config to the ESP32 device via POST /api/config."""
    r = _device_request("POST", f"http://{device_ip}/api/config", json=config)
    if r is not None:
        result = r.json()
        return result.get("ok", False)
    return False


def get_device_status(device_ip: str) -> Optional[dict]:
    """Query the ESP32 device status API."""
    r = _device_request("GET", f"http://{device_ip}/api/status")
    if r is not None:
        return r.json()
    return None


def get_device_light_state(device_ip: str) -> Optional[list]:
    """Query the ESP32 device /api/lights/state for current per-light RGB."""
    r = _device_request("GET", f"http://{device_ip}/api/lights/state")
    if r is not None:
        data = r.json()
        return data.get("lights", [])
    return None


# The config we push to the device to ensure a known-good test state.
# Light 1: RGB at DMX address 1, Light 2: RGBW at DMX address 4.
# ArtNet output on universe 0.
TEST_CONFIG = {
    "output": {
        "mode": "artnet",
        "txPin": 2,
        "enPin": 4,
        "artnetUniverse": 0
    },
    "lights": [
        {
            "name": "Test Light 1",
            "type": "RGB",
            "dmxAddr": 1,
            "channelMap": {"r": 0, "g": 1, "b": 2}
        },
        {
            "name": "Test Light 2",
            "type": "RGBW",
            "dmxAddr": 4,
            "channelMap": {"r": 0, "g": 1, "b": 2, "w": 3}
        }
    ]
}


def get_local_ip_for_device(device_ip: str) -> Optional[str]:
    """Determine which local IP would be used to reach the device."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((device_ip, 1))  # doesn't actually send anything
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return None


def configure_device_for_test(device_ip: str, universe: int = 0,
                              listen_ip: str = "0.0.0.0") -> Optional[dict]:
    """
    Ensure the device is configured for ArtNet output with a known test light.
    Sets artnetTargetIp to our local IP so the device sends unicast to us
    (broadcast UDP is unreliable under WiFi/802.15.4 coexistence).
    Returns the active config after setup, or None on failure.
    """
    print(f"\nConfiguring device at {device_ip} for integration test...")

    # Determine our local IP to receive ArtNet packets
    if listen_ip and listen_ip != "0.0.0.0":
        our_ip = listen_ip
    else:
        our_ip = get_local_ip_for_device(device_ip)
    if not our_ip:
        print("  WARNING: Could not determine local IP for ArtNet target")
        our_ip = ""

    # Read current config
    current = get_device_config(device_ip)
    if current is None:
        print("  ERROR: Cannot read device config.", file=sys.stderr)
        return None

    current_output = current.get("output", {})
    current_mode = current_output.get("mode", "dmx")
    current_universe = current_output.get("artnetUniverse", 0)
    current_target = current_output.get("artnetTargetIp", "")
    current_lights = current.get("lights", [])

    # Check if config already matches
    needs_update = False
    if current_mode != "artnet":
        print(f"  Output mode is '{current_mode}', switching to 'artnet'")
        needs_update = True
    if current_universe != universe:
        print(f"  ArtNet universe is {current_universe}, switching to {universe}")
        needs_update = True
    if our_ip and current_target != our_ip:
        print(f"  ArtNet target is '{current_target}', switching to '{our_ip}'")
        needs_update = True
    if len(current_lights) == 0:
        print(f"  No lights configured, adding test light")
        needs_update = True

    if needs_update:
        # Build config: keep existing lights if any, otherwise use test default
        config = dict(TEST_CONFIG)
        config["output"] = dict(TEST_CONFIG["output"])
        config["output"]["artnetUniverse"] = universe
        config["output"]["artnetTargetIp"] = our_ip
        if len(current_lights) > 0:
            # Keep existing lights, just fix the output mode
            config["lights"] = current_lights

        print(f"  Pushing config: {len(config['lights'])} light(s), "
              f"artnet universe={universe}, target={our_ip}")
        if not set_device_config(device_ip, config):
            print("  ERROR: Failed to push config to device.", file=sys.stderr)
            return None

        # Give device time to reconfigure DMX output
        print("  Waiting 3s for device to reconfigure...")
        time.sleep(3)

        # Re-read to confirm
        config = get_device_config(device_ip)
        if config is None:
            print("  ERROR: Cannot re-read device config after update.",
                  file=sys.stderr)
            return None
        mode = config.get("output", {}).get("mode", "dmx")
        if mode != "artnet":
            print(f"  ERROR: Device still in '{mode}' mode after config push.",
                  file=sys.stderr)
            return None
        print(f"  Device configured: mode=artnet, "
              f"universe={config['output'].get('artnetUniverse', 0)}, "
              f"target={config['output'].get('artnetTargetIp', '')}, "
              f"{len(config.get('lights', []))} light(s)")
    else:
        print(f"  Device already configured for ArtNet (universe={universe}, "
              f"{len(current_lights)} light(s))")
        config = current

    return config


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

def read_dmx_rgb(listener: ArtNetListener, dmx_start: int,
                 channel_map: dict, settle_time: float) -> Optional[tuple]:
    """
    Wait for a fresh ArtNet packet and extract the RGB DMX values.
    Returns (dmx_r, dmx_g, dmx_b) or None if no packet received.
    """
    dmx = listener.wait_for_fresh_data(timeout=settle_time + 2.0)
    if dmx is None:
        return None
    r_ch = dmx_start + channel_map.get("r", 0) - 1  # 0-indexed
    g_ch = dmx_start + channel_map.get("g", 1) - 1
    b_ch = dmx_start + channel_map.get("b", 2) - 1
    dmx_r = dmx[r_ch] if r_ch < len(dmx) else 0
    dmx_g = dmx[g_ch] if g_ch < len(dmx) else 0
    dmx_b = dmx[b_ch] if b_ch < len(dmx) else 0
    return (dmx_r, dmx_g, dmx_b)


def read_dmx_rgbw(listener: ArtNetListener, dmx_start: int,
                  channel_map: dict, settle_time: float) -> Optional[tuple]:
    """
    Wait for a fresh ArtNet packet and extract RGBW DMX values.
    Returns (dmx_r, dmx_g, dmx_b, dmx_w) or None if no packet received.
    """
    dmx = listener.wait_for_fresh_data(timeout=settle_time + 2.0)
    if dmx is None:
        return None
    r_ch = dmx_start + channel_map.get("r", 0) - 1  # 0-indexed
    g_ch = dmx_start + channel_map.get("g", 1) - 1
    b_ch = dmx_start + channel_map.get("b", 2) - 1
    w_ch = dmx_start + channel_map.get("w", 3) - 1
    dmx_r = dmx[r_ch] if r_ch < len(dmx) else 0
    dmx_g = dmx[g_ch] if g_ch < len(dmx) else 0
    dmx_b = dmx[b_ch] if b_ch < len(dmx) else 0
    dmx_w = dmx[w_ch] if w_ch < len(dmx) else 0
    return (dmx_r, dmx_g, dmx_b, dmx_w)


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


def decompose_rgbw(r: int, g: int, b: int) -> tuple:
    """
    RGB-to-RGBW decomposition: extract the white component.
    Matches firmware's decomposeRGBW() in zigbee_manager.cpp.

    The white component is min(R, G, B), subtracted from each RGB channel.
    Returns (r', g', b', w).
    """
    w = min(r, g, b)
    return (r - w, g - w, b - w, w)


def compute_expected_dmx_rgbw(r: int, g: int, b: int, brightness: int,
                               power_on: bool) -> tuple:
    """
    Compute expected RGBW DMX values after decomposition and brightness scaling.
    Matches firmware pipeline: decomposeRGBW() then brightness scaling in dmx_output.

    Note: The firmware applies decomposition BEFORE storing state, and brightness
    scaling happens in dmx_output. So the input r,g,b here should be the raw
    (pre-decomposition) values from xyToRGB/mirekToRGB.

    Returns (dmx_r, dmx_g, dmx_b, dmx_w)
    """
    if not power_on:
        return (0, 0, 0, 0)
    r2, g2, b2, w = decompose_rgbw(r, g, b)
    bri_scale = brightness / 254.0
    return (int(r2 * bri_scale), int(g2 * bri_scale),
            int(b2 * bri_scale), int(w * bri_scale))


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
                      settle_time: float, light_type: str = "RGB"):
    """Test turning a light on and off."""
    print(f"\n--- Test: ON/OFF for light #{light_id} ---")
    is_rgbw = (light_type == "RGBW")

    # Turn ON, full brightness, white
    print("  Setting light ON, brightness=254, white...")
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.3127, 0.3290]})
    time.sleep(settle_time)

    state = api.get_light(light_id).get("state", {})
    results.check(f"Light {light_id} ON: Hue reports on=True",
                  True, state.get("on"))

    if listener:
        if is_rgbw:
            rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
            if rgbw is None:
                results.check(f"Light {light_id} ON: ArtNet packet received", True, False)
            else:
                dmx_r, dmx_g, dmx_b, dmx_w = rgbw
                print(f"  ArtNet DMX values: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")
                # For RGBW white, most output is on W channel; total should be > 200
                total = dmx_r + dmx_g + dmx_b + dmx_w
                results.check(f"Light {light_id} ON: RGBW total output > 200",
                              True, total > 200)
                results.check(f"Light {light_id} ON: RGBW W channel > 200",
                              True, dmx_w > 200)
        else:
            rgb = read_dmx_rgb(listener, dmx_start, channel_map, settle_time)
            if rgb is None:
                results.check(f"Light {light_id} ON: ArtNet packet received", True, False)
            else:
                dmx_r, dmx_g, dmx_b = rgb
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
        if is_rgbw:
            rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
            if rgbw is None:
                results.check(f"Light {light_id} OFF: ArtNet packet received", True, False)
            else:
                dmx_r, dmx_g, dmx_b, dmx_w = rgbw
                print(f"  ArtNet DMX values: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")
                results.check(f"Light {light_id} OFF: DMX R == 0", 0, dmx_r)
                results.check(f"Light {light_id} OFF: DMX G == 0", 0, dmx_g)
                results.check(f"Light {light_id} OFF: DMX B == 0", 0, dmx_b)
                results.check(f"Light {light_id} OFF: DMX W == 0", 0, dmx_w)
        else:
            rgb = read_dmx_rgb(listener, dmx_start, channel_map, settle_time)
            if rgb is None:
                results.check(f"Light {light_id} OFF: ArtNet packet received", True, False)
            else:
                dmx_r, dmx_g, dmx_b = rgb
                print(f"  ArtNet DMX values: R={dmx_r} G={dmx_g} B={dmx_b}")
                results.check(f"Light {light_id} OFF: DMX R == 0", 0, dmx_r)
                results.check(f"Light {light_id} OFF: DMX G == 0", 0, dmx_g)
                results.check(f"Light {light_id} OFF: DMX B == 0", 0, dmx_b)


def test_light_color(api: HueAPI, light_id: str, listener: Optional[ArtNetListener],
                     dmx_start: int, channel_map: dict, results: TestResult,
                     settle_time: float):
    """Test setting specific colors via CIE xy coordinates.

    NOTE: The Hue Bridge gamut-maps xy coordinates before sending ZCL
    commands, so the actual color the device receives may differ from
    what we requested.  We verify:
    1. The Bridge reports xy close to what we set
    2. The DMX output shows a color change
    3. The intended channel is significant (>100)
    """
    print(f"\n--- Test: COLOR for light #{light_id} ---")

    # Test cases: (name, hue_xy, expected_significant_channel)
    test_colors = [
        ("Red",   [0.6750, 0.3220], "r"),  # Saturated red
        ("Green", [0.4091, 0.5180], "g"),  # Saturated green
        ("Blue",  [0.1670, 0.0400], "b"),  # Saturated blue
    ]

    # Ensure light is on at full brightness
    api.set_light_state(light_id, {"on": True, "bri": 254})
    time.sleep(0.5)

    for color_name, xy, significant_ch in test_colors:
        print(f"  Setting color: {color_name} (xy={xy})...")
        api.set_light_state(light_id, {"xy": xy})
        time.sleep(settle_time)

        # Verify Hue reports the color (read back — Bridge may gamut-clip)
        state = api.get_light(light_id).get("state", {})
        reported_xy = state.get("xy", [0, 0])
        results.check(f"Light {light_id} {color_name}: Hue xy[0] ~= {xy[0]:.3f}",
                      xy[0], reported_xy[0], tolerance=0.05)
        results.check(f"Light {light_id} {color_name}: Hue xy[1] ~= {xy[1]:.3f}",
                      xy[1], reported_xy[1], tolerance=0.05)

        if listener:
            rgb = read_dmx_rgb(listener, dmx_start, channel_map, settle_time)
            if rgb is None:
                results.check(f"Light {light_id} {color_name}: ArtNet received",
                              True, False)
                continue

            dmx_r, dmx_g, dmx_b = rgb
            print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b}")

            channels = {"r": dmx_r, "g": dmx_g, "b": dmx_b}

            # The intended channel should be significant (>100).
            # Bridge gamut-mapping may cause other channels to also be
            # high, so we don't assert it's the absolute maximum.
            results.check(
                f"Light {light_id} {color_name}: '{significant_ch}' channel > 100",
                True, channels[significant_ch] > 100)

            # At least one channel should be at full brightness (255)
            max_val = max(channels.values())
            results.check(
                f"Light {light_id} {color_name}: max channel == 255",
                True, max_val >= 250)


def test_light_brightness(api: HueAPI, light_id: str, listener: Optional[ArtNetListener],
                          dmx_start: int, channel_map: dict, results: TestResult,
                          settle_time: float, light_type: str = "RGB"):
    """Test brightness scaling."""
    print(f"\n--- Test: BRIGHTNESS for light #{light_id} ---")
    is_rgbw = (light_type == "RGBW")

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
            if is_rgbw:
                rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
                if rgbw is None:
                    results.check(f"Light {light_id} bri={bri}: ArtNet received",
                                  True, False)
                    continue
                dmx_r, dmx_g, dmx_b, dmx_w = rgbw
                print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")

                # For RGBW white, the W channel carries most of the output.
                # W should be ~255 * (bri/254), RGB residual should be small.
                expected_w_approx = int(255 * (bri / 254.0))
                results.check(f"Light {light_id} bri={bri}: RGBW W ~= {expected_w_approx}",
                              expected_w_approx, dmx_w, tolerance=20)
            else:
                rgb = read_dmx_rgb(listener, dmx_start, channel_map, settle_time)
                if rgb is None:
                    results.check(f"Light {light_id} bri={bri}: ArtNet received",
                                  True, False)
                    continue

                dmx_r, dmx_g, dmx_b = rgb
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
                               results: TestResult, settle_time: float,
                               device_ip: Optional[str] = None,
                               light_index: int = 0):
    """
    Test that the color pipeline from Hue API -> Zigbee -> DMX is consistent.

    IMPORTANT: The Hue Bridge gamut-maps xy coordinates before sending ZCL
    commands, and the ZCL values may differ significantly from the REST API
    values.  We therefore query the device's own /api/lights/state to get
    the RGB the firmware actually computed, and verify the ArtNet DMX output
    matches that.  This tests: device state -> DMX output fidelity.
    """
    print(f"\n--- Test: COLOR ACCURACY for light #{light_id} ---")

    api.set_light_state(light_id, {"on": True, "bri": 254})
    time.sleep(0.5)

    # Test cases: (name, requested_xy)
    test_cases = [
        ("White D65",   [0.3127, 0.3290]),
        ("Red",         [0.6750, 0.3220]),
        ("Green",       [0.4091, 0.5180]),
        ("Blue",        [0.1670, 0.0400]),
        ("Warm White",  [0.4578, 0.4101]),
        ("Magenta",     [0.3833, 0.1591]),
    ]

    for name, xy in test_cases:
        print(f"  Testing {name} (requested xy={xy})...")
        api.set_light_state(light_id, {"xy": xy})
        time.sleep(settle_time)

        if not listener:
            continue

        # Read the device's own computed RGB (what it received via ZCL)
        device_rgb = None
        if device_ip:
            dev_state = get_device_light_state(device_ip)
            if dev_state and light_index < len(dev_state):
                ls = dev_state[light_index]
                device_rgb = (ls.get("r", 0), ls.get("g", 0), ls.get("b", 0))
                print(f"    Device reports RGB: R={device_rgb[0]} G={device_rgb[1]} "
                      f"B={device_rgb[2]} (on={ls.get('on')}, bri={ls.get('bri')})")

        rgb = read_dmx_rgb(listener, dmx_start, channel_map, settle_time)
        if rgb is None:
            results.check(f"Light {light_id} {name}: ArtNet received", True, False)
            continue

        dmx_r, dmx_g, dmx_b = rgb
        print(f"    ArtNet   DMX: R={dmx_r} G={dmx_g} B={dmx_b}")

        if device_rgb:
            # Compare ArtNet output against what the device itself reports
            exp_dmx_r, exp_dmx_g, exp_dmx_b = device_rgb
            print(f"    Expected DMX: R={exp_dmx_r} G={exp_dmx_g} B={exp_dmx_b} "
                  f"(from device state)")
            results.check(f"Light {light_id} {name}: R accuracy",
                          exp_dmx_r, dmx_r, tolerance=3)
            results.check(f"Light {light_id} {name}: G accuracy",
                          exp_dmx_g, dmx_g, tolerance=3)
            results.check(f"Light {light_id} {name}: B accuracy",
                          exp_dmx_b, dmx_b, tolerance=3)
        else:
            # Fallback: compute from Bridge-reported xy (less accurate)
            state = api.get_light(light_id).get("state", {})
            reported_xy = state.get("xy", xy)
            reported_bri = state.get("bri", 254)
            exp_r, exp_g, exp_b = cie_xy_to_rgb(reported_xy[0], reported_xy[1])
            bri_scale = reported_bri / 254.0
            exp_dmx_r = int(exp_r * bri_scale)
            exp_dmx_g = int(exp_g * bri_scale)
            exp_dmx_b = int(exp_b * bri_scale)
            print(f"    Expected DMX: R={exp_dmx_r} G={exp_dmx_g} B={exp_dmx_b} "
                  f"(from Bridge xy, less reliable)")
            results.check(f"Light {light_id} {name}: R accuracy",
                          exp_dmx_r, dmx_r, tolerance=15)
            results.check(f"Light {light_id} {name}: G accuracy",
                          exp_dmx_g, dmx_g, tolerance=15)
            results.check(f"Light {light_id} {name}: B accuracy",
                          exp_dmx_b, dmx_b, tolerance=15)


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
                                  results: TestResult, settle_time: float,
                                  light_type: str = "RGB"):
    """
    Test color temperature (mirek) control via the Hue API 'ct' parameter.
    Verifies that the firmware's mirekToRGB conversion produces expected DMX values.
    For RGBW lights, accounts for white channel decomposition.
    """
    print(f"\n--- Test: COLOR TEMPERATURE for light #{light_id} ---")
    is_rgbw = (light_type == "RGBW")

    # Check if the bridge thinks this light supports color temperature
    light_info = api.get_light(light_id)
    hue_light_type = light_info.get("type", "")
    capabilities = light_info.get("capabilities", {})
    ct_cap = capabilities.get("control", {}).get("ct")

    if hue_light_type == "Color light" or ct_cap is None:
        print(f"  SKIP: Bridge reports type='{hue_light_type}' with no CT capability.")
        print(f"  The device needs to be re-paired for the bridge to discover")
        print(f"  the updated color temperature attributes.")
        print(f"  Steps: Delete light #{light_id} from bridge -> re-pair device")
        results.check(f"Light {light_id} CT: bridge reports CT capability",
                      True, False)
        return

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

        # Check if the Hue API reported an error (e.g. parameter not available)
        has_error = False
        if isinstance(response, list):
            for item in response:
                if "error" in item:
                    has_error = True
                    break
        if has_error:
            results.check(f"Light {light_id} CT {name}: Hue API accepted ct parameter",
                          True, False)
            continue

        time.sleep(settle_time)

        # Verify Hue reports the color temperature
        state = api.get_light(light_id).get("state", {})
        reported_ct = state.get("ct")
        if reported_ct is None or reported_ct == 0:
            print(f"    WARNING: Bridge reports ct={reported_ct} - CT not supported?")
            results.check(f"Light {light_id} CT {name}: Hue reports ct ~= {mirek}",
                          mirek, reported_ct or 0, tolerance=5)
            continue

        results.check(f"Light {light_id} CT {name}: Hue reports ct ~= {mirek}",
                      mirek, reported_ct, tolerance=5)

        if not listener:
            continue

        # Compute expected RGB from our Python port of the firmware
        exp_r, exp_g, exp_b = mirek_to_rgb(mirek)

        if is_rgbw:
            rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
            if rgbw is None:
                results.check(f"Light {light_id} CT {name}: ArtNet received",
                              True, False)
                continue

            dmx_r, dmx_g, dmx_b, dmx_w = rgbw

            # Apply decomposition to expected values
            exp_r2, exp_g2, exp_b2, exp_w = decompose_rgbw(exp_r, exp_g, exp_b)

            print(f"    Expected DMX: R={exp_r2} G={exp_g2} B={exp_b2} W={exp_w}")
            print(f"    Actual   DMX: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")

            results.check(f"Light {light_id} CT {name}: R accuracy",
                          exp_r2, dmx_r, tolerance=10)
            results.check(f"Light {light_id} CT {name}: G accuracy",
                          exp_g2, dmx_g, tolerance=10)
            results.check(f"Light {light_id} CT {name}: B accuracy",
                          exp_b2, dmx_b, tolerance=10)
            results.check(f"Light {light_id} CT {name}: W accuracy",
                          exp_w, dmx_w, tolerance=10)

            # Verify warm is more red-dominant, cool has high W
            if mirek >= 370:
                results.check(f"Light {light_id} CT {name}: warm has R > B",
                              True, dmx_r >= dmx_b)
            elif mirek <= 200:
                results.check(f"Light {light_id} CT {name}: cool has high W",
                              True, dmx_w > 150)
        else:
            rgb = read_dmx_rgb(listener, dmx_start, channel_map, settle_time)
            if rgb is None:
                results.check(f"Light {light_id} CT {name}: ArtNet received",
                              True, False)
                continue

            dmx_r, dmx_g, dmx_b = rgb

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


def test_rgbw_decomposition(api: HueAPI, light_id: str,
                             listener: Optional[ArtNetListener],
                             dmx_start: int, channel_map: dict,
                             results: TestResult, settle_time: float,
                             device_ip: Optional[str] = None,
                             light_index: int = 0):
    """
    Test RGB-to-RGBW white channel decomposition via ArtNet output.

    For RGBW lights, the firmware decomposes RGB into RGBW by extracting
    the white component: W = min(R,G,B), then R'=R-W, G'=G-W, B'=B-W.
    This test verifies that the 4th DMX channel (white) is correctly populated.

    Key test cases:
    - Pure white (R=G=B=255) -> R'=0, G'=0, B'=0, W=255
    - Saturated red (R=255, G~=0, B~=0) -> R'=255, G'=0, B'=0, W~=0
    - Pastel / desaturated color -> non-zero W extracted
    - Color temperature -> warm whites should decompose with high W
    """
    print(f"\n--- Test: RGBW DECOMPOSITION for light #{light_id} ---")

    # Ensure light is on at full brightness
    api.set_light_state(light_id, {"on": True, "bri": 254})
    time.sleep(0.5)

    if not listener:
        print("  SKIP: No ArtNet listener (RGBW tests require ArtNet)")
        return

    # ---- Test 1: Pure white -> all white channel, no RGB ----
    print("  Test 1: Pure white (xy=D65) -> W=max, RGB=0")
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.3127, 0.3290]})
    time.sleep(settle_time)

    rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
    if rgbw is None:
        results.check(f"Light {light_id} RGBW white: ArtNet received", True, False)
    else:
        dmx_r, dmx_g, dmx_b, dmx_w = rgbw
        print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")

        # For pure white, the device receives R=G=B=255 from xyToRGB.
        # Decomposition: W=255, R'=0, G'=0, B'=0.
        # After brightness scaling (254/254 ~ 1.0): expect W~=255, RGB~=0.
        # However, the actual values depend on what the Bridge sends via ZCL
        # (gamut mapping may shift xy). We verify via device state if available.
        if device_ip:
            dev_state = get_device_light_state(device_ip)
            if dev_state and light_index < len(dev_state):
                ls = dev_state[light_index]
                dev_r = ls.get("r", 0)
                dev_g = ls.get("g", 0)
                dev_b = ls.get("b", 0)
                dev_w = ls.get("w", 0)
                print(f"    Device reports: R={dev_r} G={dev_g} B={dev_b} W={dev_w}")
                results.check(f"Light {light_id} RGBW white: DMX R matches device",
                              dev_r, dmx_r, tolerance=3)
                results.check(f"Light {light_id} RGBW white: DMX G matches device",
                              dev_g, dmx_g, tolerance=3)
                results.check(f"Light {light_id} RGBW white: DMX B matches device",
                              dev_b, dmx_b, tolerance=3)
                results.check(f"Light {light_id} RGBW white: DMX W matches device",
                              dev_w, dmx_w, tolerance=3)

        # White channel should be high (> 200)
        results.check(f"Light {light_id} RGBW white: W channel > 200",
                      True, dmx_w > 200)
        # RGB channels should be low (white is fully extracted)
        # Note: gamut mapping may not give perfect R=G=B from xy D65,
        # so the residual RGB might not be exactly 0 but should be small
        results.check(f"Light {light_id} RGBW white: R channel < 30",
                      True, dmx_r < 30)
        results.check(f"Light {light_id} RGBW white: G channel < 30",
                      True, dmx_g < 30)
        results.check(f"Light {light_id} RGBW white: B channel < 30",
                      True, dmx_b < 30)

    # ---- Test 2: Saturated red -> no white channel ----
    print("  Test 2: Saturated red -> W=0, R=max")
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.6750, 0.3220]})
    time.sleep(settle_time)

    rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
    if rgbw is None:
        results.check(f"Light {light_id} RGBW red: ArtNet received", True, False)
    else:
        dmx_r, dmx_g, dmx_b, dmx_w = rgbw
        print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")

        # Saturated red: R=255, G~=0, B~=0 -> W=min(255,~0,~0)=~0
        results.check(f"Light {light_id} RGBW red: R channel > 200",
                      True, dmx_r > 200)
        # White should be very low for saturated colors
        results.check(f"Light {light_id} RGBW red: W channel < 30",
                      True, dmx_w < 30)

    # ---- Test 3: Saturated green -> no white channel ----
    print("  Test 3: Saturated green -> W low, G=max")
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.4091, 0.5180]})
    time.sleep(settle_time)

    rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
    if rgbw is None:
        results.check(f"Light {light_id} RGBW green: ArtNet received", True, False)
    else:
        dmx_r, dmx_g, dmx_b, dmx_w = rgbw
        print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")

        results.check(f"Light {light_id} RGBW green: G channel > 200",
                      True, dmx_g > 200)
        # White should be low for saturated colors
        results.check(f"Light {light_id} RGBW green: W channel < 50",
                      True, dmx_w < 50)

    # ---- Test 4: Saturated blue -> no white channel ----
    print("  Test 4: Saturated blue -> W low, B=max")
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.1670, 0.0400]})
    time.sleep(settle_time)

    rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
    if rgbw is None:
        results.check(f"Light {light_id} RGBW blue: ArtNet received", True, False)
    else:
        dmx_r, dmx_g, dmx_b, dmx_w = rgbw
        print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")

        results.check(f"Light {light_id} RGBW blue: B channel > 200",
                      True, dmx_b > 200)
        results.check(f"Light {light_id} RGBW blue: W channel < 30",
                      True, dmx_w < 30)

    # ---- Test 5: Warm white via color temperature -> high W ----
    # Check if the bridge supports CT for this light first
    light_info = api.get_light(light_id)
    ct_cap = light_info.get("capabilities", {}).get("control", {}).get("ct")
    if ct_cap is not None:
        print("  Test 5: Warm white via CT=370 -> high W")
        api.set_light_state(light_id, {"on": True, "bri": 254, "ct": 370})
        time.sleep(settle_time)

        rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
        if rgbw is None:
            results.check(f"Light {light_id} RGBW warm CT: ArtNet received",
                          True, False)
        else:
            dmx_r, dmx_g, dmx_b, dmx_w = rgbw
            print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")

            # Warm white (2703K) from mirekToRGB gives R=255 G=168 B=71.
            # Decomposition: W=71, R'=184, G'=97, B'=0.
            # The white channel should be meaningful (> 30)
            results.check(f"Light {light_id} RGBW warm CT: W channel > 30",
                          True, dmx_w > 30)
            # R+W combined should represent warm tone (R dominant)
            results.check(f"Light {light_id} RGBW warm CT: R+W > G+W (warm tone)",
                          True, (dmx_r + dmx_w) >= (dmx_g + dmx_w))

        print("  Test 6: Cool daylight via CT=153 -> W present")
        api.set_light_state(light_id, {"on": True, "bri": 254, "ct": 153})
        time.sleep(settle_time)

        rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
        if rgbw is None:
            results.check(f"Light {light_id} RGBW cool CT: ArtNet received",
                          True, False)
        else:
            dmx_r, dmx_g, dmx_b, dmx_w = rgbw
            print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")

            # Cool daylight (6536K) from mirekToRGB gives R=255 G=249 B=253.
            # Decomposition: W=249, R'=6, G'=0, B'=4. High white content.
            results.check(f"Light {light_id} RGBW cool CT: W channel > 150",
                          True, dmx_w > 150)
    else:
        print("  SKIP: Tests 5-6 (CT not supported by bridge for this light)")

    # ---- Test 7: Light OFF -> all channels zero including W ----
    print("  Test 7: Light OFF -> all RGBW channels = 0")
    api.set_light_state(light_id, {"on": False})
    time.sleep(settle_time)

    rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
    if rgbw is None:
        results.check(f"Light {light_id} RGBW off: ArtNet received", True, False)
    else:
        dmx_r, dmx_g, dmx_b, dmx_w = rgbw
        print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")
        results.check(f"Light {light_id} RGBW off: R == 0", 0, dmx_r)
        results.check(f"Light {light_id} RGBW off: G == 0", 0, dmx_g)
        results.check(f"Light {light_id} RGBW off: B == 0", 0, dmx_b)
        results.check(f"Light {light_id} RGBW off: W == 0", 0, dmx_w)

    # ---- Test 8: Half brightness white -> W scaled, RGB low ----
    print("  Test 8: Half brightness white -> W scaled")
    api.set_light_state(light_id, {"on": True, "bri": 127, "xy": [0.3127, 0.3290]})
    time.sleep(settle_time)

    rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
    if rgbw is None:
        results.check(f"Light {light_id} RGBW half-bri: ArtNet received", True, False)
    else:
        dmx_r, dmx_g, dmx_b, dmx_w = rgbw
        print(f"    ArtNet DMX: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")

        # At bri=127 (~50%), W should be roughly half of full brightness W
        # Full brightness W was ~255, so half should be ~127
        results.check(f"Light {light_id} RGBW half-bri: W channel 90-160",
                      True, 90 <= dmx_w <= 160)
        # RGB should still be low (white extracted)
        results.check(f"Light {light_id} RGBW half-bri: R channel < 20",
                      True, dmx_r < 20)

    # Restore
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.3127, 0.3290]})


def test_rgbw_accuracy(api: HueAPI, light_id: str,
                        listener: Optional[ArtNetListener],
                        dmx_start: int, channel_map: dict,
                        results: TestResult, settle_time: float,
                        device_ip: Optional[str] = None,
                        light_index: int = 0):
    """
    Test RGBW accuracy: verify ArtNet RGBW output matches the device's own
    reported state (which includes the decomposed white channel).
    """
    print(f"\n--- Test: RGBW ACCURACY for light #{light_id} ---")

    if not listener:
        print("  SKIP: No ArtNet listener")
        return

    api.set_light_state(light_id, {"on": True, "bri": 254})
    time.sleep(0.5)

    # Test cases: (name, hue_state)
    test_cases = [
        ("White D65",   {"xy": [0.3127, 0.3290]}),
        ("Red",         {"xy": [0.6750, 0.3220]}),
        ("Green",       {"xy": [0.4091, 0.5180]}),
        ("Blue",        {"xy": [0.1670, 0.0400]}),
        ("Warm White",  {"xy": [0.4578, 0.4101]}),
        ("Magenta",     {"xy": [0.3833, 0.1591]}),
    ]

    for name, hue_state in test_cases:
        print(f"  Testing {name}...")
        api.set_light_state(light_id, hue_state)
        time.sleep(settle_time)

        # Read device's own state (includes decomposed RGBW)
        device_rgbw = None
        if device_ip:
            dev_state = get_device_light_state(device_ip)
            if dev_state and light_index < len(dev_state):
                ls = dev_state[light_index]
                device_rgbw = (ls.get("r", 0), ls.get("g", 0),
                               ls.get("b", 0), ls.get("w", 0))
                print(f"    Device: R={device_rgbw[0]} G={device_rgbw[1]} "
                      f"B={device_rgbw[2]} W={device_rgbw[3]}")

        rgbw = read_dmx_rgbw(listener, dmx_start, channel_map, settle_time)
        if rgbw is None:
            results.check(f"Light {light_id} RGBW {name}: ArtNet received",
                          True, False)
            continue

        dmx_r, dmx_g, dmx_b, dmx_w = rgbw
        print(f"    ArtNet: R={dmx_r} G={dmx_g} B={dmx_b} W={dmx_w}")

        if device_rgbw:
            exp_r, exp_g, exp_b, exp_w = device_rgbw
            results.check(f"Light {light_id} RGBW {name}: R accuracy",
                          exp_r, dmx_r, tolerance=3)
            results.check(f"Light {light_id} RGBW {name}: G accuracy",
                          exp_g, dmx_g, tolerance=3)
            results.check(f"Light {light_id} RGBW {name}: B accuracy",
                          exp_b, dmx_b, tolerance=3)
            results.check(f"Light {light_id} RGBW {name}: W accuracy",
                          exp_w, dmx_w, tolerance=3)
        else:
            # Without device state, just verify invariant: R+W, G+W, B+W
            # should reconstruct a reasonable color
            total = dmx_r + dmx_g + dmx_b + dmx_w
            results.check(f"Light {light_id} RGBW {name}: total output > 0",
                          True, total > 0)


# ---------------------------------------------------------------------------
#  SSE (Server-Sent Events) test helpers and scenarios
# ---------------------------------------------------------------------------

class SSEClient:
    """
    Minimal SSE client using requests with stream=True.
    Parses named events (event: / data:) and comments (lines starting with :).
    Uses the same retry/timeout strategy as other device requests.
    """

    def __init__(self, url: str, timeout: int = DEVICE_TIMEOUT):
        self.url = url
        self.timeout = timeout
        self.response = None
        self.events: list = []       # list of (event_name, data_json_str)
        self.comments: list = []     # list of comment strings (keepalive etc.)
        self._thread = None
        self._running = False
        self._lock = threading.Lock()

    def connect(self) -> bool:
        """Open the SSE stream. Returns True on success."""
        try:
            self.response = requests.get(
                self.url, stream=True, timeout=self.timeout)
            self.response.raise_for_status()
        except Exception as e:
            print(f"  SSE connect failed: {e}")
            return False
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return True

    def close(self):
        """Close the SSE stream."""
        self._running = False
        if self.response:
            try:
                self.response.close()
            except Exception:
                pass
            self.response = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _read_loop(self):
        """Background thread: read lines from the chunked HTTP response."""
        current_event = None
        current_data = None
        try:
            for line_bytes in self.response.iter_lines():
                if not self._running:
                    break
                line = line_bytes.decode("utf-8", errors="replace")

                if line.startswith(":"):
                    # SSE comment (e.g. ": keepalive")
                    with self._lock:
                        self.comments.append(line)
                elif line.startswith("event: "):
                    current_event = line[7:]
                elif line.startswith("data: "):
                    current_data = line[6:]
                elif line == "":
                    # Blank line = end of event
                    if current_event and current_data is not None:
                        with self._lock:
                            self.events.append((current_event, current_data))
                    current_event = None
                    current_data = None
        except Exception:
            # Connection closed or error — expected on disconnect
            pass

    def wait_for_events(self, count: int, timeout: float = 10.0) -> list:
        """Wait until at least `count` events are collected, return all events."""
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                if len(self.events) >= count:
                    return list(self.events)
            time.sleep(0.2)
        with self._lock:
            return list(self.events)

    def wait_for_event_named(self, name: str, min_index: int = 0,
                             timeout: float = 10.0) -> Optional[str]:
        """Wait for an event with the given name (at index >= min_index).
        Returns the data string, or None on timeout."""
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                for i in range(min_index, len(self.events)):
                    if self.events[i][0] == name:
                        return self.events[i][1]
            time.sleep(0.2)
        return None

    def wait_for_comment(self, timeout: float = 20.0) -> bool:
        """Wait for at least one SSE comment (keepalive). Returns True if received."""
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                if self.comments:
                    return True
            time.sleep(0.5)
        return False

    def event_count(self) -> int:
        with self._lock:
            return len(self.events)

    def clear(self):
        with self._lock:
            self.events.clear()
            self.comments.clear()


def test_sse_initial_events(device_ip: str, results: TestResult,
                            settle_time: float):
    """
    Test that connecting to /api/events immediately delivers a 'status'
    and a 'lightstate' event with valid JSON payloads.
    """
    print(f"\n--- Test: SSE INITIAL EVENTS ---")

    sse = SSEClient(f"http://{device_ip}/api/events")
    if not sse.connect():
        results.check("SSE initial: connect", True, False)
        return

    try:
        # Wait for at least 2 events (status + lightstate)
        events = sse.wait_for_events(2, timeout=settle_time + 5)
        results.check("SSE initial: received >= 2 events", True, len(events) >= 2)

        if len(events) < 2:
            print(f"  Only received {len(events)} event(s)")
            return

        # First event should be 'status'
        evt_name, evt_data = events[0]
        results.check("SSE initial: first event is 'status'", "status", evt_name)

        try:
            status = json.loads(evt_data)
            results.check("SSE initial: status has 'wifi' field",
                          True, "wifi" in status)
            results.check("SSE initial: status has 'zigbee' field",
                          True, "zigbee" in status)
            results.check("SSE initial: status has 'lightCount' field",
                          True, "lightCount" in status)
            results.check("SSE initial: status has 'eui64' field",
                          True, "eui64" in status)
            print(f"  Status: wifi={status.get('wifi')}, "
                  f"zigbee={status.get('zigbee')}, "
                  f"lights={status.get('lightCount')}")
        except json.JSONDecodeError as e:
            results.check(f"SSE initial: status is valid JSON", True, False)
            print(f"  Bad JSON: {e}")

        # Second event should be 'lightstate'
        evt_name, evt_data = events[1]
        results.check("SSE initial: second event is 'lightstate'",
                      "lightstate", evt_name)

        try:
            lightstate = json.loads(evt_data)
            results.check("SSE initial: lightstate has 'lights' array",
                          True, "lights" in lightstate
                          and isinstance(lightstate["lights"], list))
            lights = lightstate.get("lights", [])
            if lights:
                first = lights[0]
                results.check("SSE initial: light[0] has 'on' field",
                              True, "on" in first)
                results.check("SSE initial: light[0] has 'bri' field",
                              True, "bri" in first)
                results.check("SSE initial: light[0] has 'r' field",
                              True, "r" in first)
                print(f"  Light[0]: on={first.get('on')}, bri={first.get('bri')}, "
                      f"r={first.get('r')} g={first.get('g')} "
                      f"b={first.get('b')} w={first.get('w')}")
        except json.JSONDecodeError as e:
            results.check("SSE initial: lightstate is valid JSON", True, False)
            print(f"  Bad JSON: {e}")
    finally:
        sse.close()


def test_sse_concurrent_rest(device_ip: str, results: TestResult,
                             settle_time: float):
    """
    Test that REST API requests succeed while an SSE connection is open.
    This verifies the async SSE handler doesn't block httpd worker threads.
    """
    print(f"\n--- Test: SSE CONCURRENT REST ---")

    sse = SSEClient(f"http://{device_ip}/api/events")
    if not sse.connect():
        results.check("SSE concurrent: connect", True, False)
        return

    try:
        # Wait for initial events to confirm SSE is established
        sse.wait_for_events(2, timeout=settle_time + 5)

        # Now make REST API requests while SSE is open
        print("  Testing GET /api/status while SSE connected...")
        status = get_device_status(device_ip)
        results.check("SSE concurrent: GET /api/status succeeded",
                      True, status is not None)
        if status:
            print(f"    Status: wifi={status.get('wifi')}, "
                  f"zigbee={status.get('zigbee')}")

        print("  Testing GET /api/lights/state while SSE connected...")
        lights = get_device_light_state(device_ip)
        results.check("SSE concurrent: GET /api/lights/state succeeded",
                      True, lights is not None)
        if lights:
            print(f"    Got {len(lights)} light state(s)")

        print("  Testing GET /api/config while SSE connected...")
        config = get_device_config(device_ip)
        results.check("SSE concurrent: GET /api/config succeeded",
                      True, config is not None)
        if config:
            print(f"    Config: {len(config.get('lights', []))} light(s)")

    finally:
        sse.close()


def test_sse_state_change(device_ip: str, api: HueAPI, light_id: str,
                          results: TestResult, settle_time: float):
    """
    Test that toggling a light via the Hue Bridge triggers an SSE
    'lightstate' event push (beyond the initial events).
    """
    print(f"\n--- Test: SSE STATE CHANGE (light #{light_id}) ---")

    sse = SSEClient(f"http://{device_ip}/api/events")
    if not sse.connect():
        results.check("SSE state change: connect", True, False)
        return

    try:
        # Wait for initial events
        events = sse.wait_for_events(2, timeout=settle_time + 5)
        initial_count = sse.event_count()
        if initial_count < 2:
            results.check("SSE state change: initial events received", True, False)
            return
        print(f"  Received {initial_count} initial events")

        # Toggle light OFF
        print(f"  Turning light #{light_id} OFF via Hue API...")
        api.set_light_state(light_id, {"on": False})
        time.sleep(settle_time)

        # Wait for a new lightstate event (beyond initial ones)
        new_event = sse.wait_for_event_named(
            "lightstate", min_index=initial_count, timeout=settle_time + 5)
        results.check("SSE state change: received 'lightstate' after OFF",
                      True, new_event is not None)

        if new_event:
            try:
                data = json.loads(new_event)
                lights = data.get("lights", [])
                if lights:
                    print(f"    Pushed light[0]: on={lights[0].get('on')}, "
                          f"bri={lights[0].get('bri')}")
                    results.check("SSE state change: light reports on=false",
                                  False, lights[0].get("on"))
            except json.JSONDecodeError:
                results.check("SSE state change: lightstate is valid JSON",
                              True, False)

        count_after_off = sse.event_count()

        # Toggle light ON
        print(f"  Turning light #{light_id} ON via Hue API...")
        api.set_light_state(light_id, {"on": True, "bri": 254})
        time.sleep(settle_time)

        new_event = sse.wait_for_event_named(
            "lightstate", min_index=count_after_off, timeout=settle_time + 5)
        results.check("SSE state change: received 'lightstate' after ON",
                      True, new_event is not None)

        if new_event:
            try:
                data = json.loads(new_event)
                lights = data.get("lights", [])
                if lights:
                    print(f"    Pushed light[0]: on={lights[0].get('on')}, "
                          f"bri={lights[0].get('bri')}")
                    results.check("SSE state change: light reports on=true",
                                  True, lights[0].get("on"))
            except json.JSONDecodeError:
                results.check("SSE state change: lightstate is valid JSON",
                              True, False)
    finally:
        sse.close()


def test_sse_keepalive(device_ip: str, results: TestResult):
    """
    Test that the SSE stream sends a keepalive comment within ~15 seconds
    of inactivity. We wait 18s to give margin.
    """
    print(f"\n--- Test: SSE KEEPALIVE ---")
    print("  (This test waits ~18 seconds for a keepalive comment)")

    sse = SSEClient(f"http://{device_ip}/api/events", timeout=30)
    if not sse.connect():
        results.check("SSE keepalive: connect", True, False)
        return

    try:
        # Wait for initial events
        sse.wait_for_events(2, timeout=10)

        # Clear comments collected during connect, then wait for keepalive
        sse.clear()
        print("  Waiting up to 18s for keepalive comment...")
        got_keepalive = sse.wait_for_comment(timeout=18)
        results.check("SSE keepalive: received comment within 18s",
                      True, got_keepalive)

        if got_keepalive:
            with sse._lock:
                print(f"    Comment: {sse.comments[0]}")
    finally:
        sse.close()


def test_sse_reconnect(device_ip: str, results: TestResult,
                       settle_time: float):
    """
    Test that after disconnecting and reconnecting, the SSE stream
    delivers fresh initial events.
    """
    print(f"\n--- Test: SSE RECONNECT ---")

    # First connection
    sse1 = SSEClient(f"http://{device_ip}/api/events")
    if not sse1.connect():
        results.check("SSE reconnect: first connect", True, False)
        return

    events1 = sse1.wait_for_events(2, timeout=settle_time + 5)
    results.check("SSE reconnect: first connection got events",
                  True, len(events1) >= 2)
    sse1.close()
    print("  First connection closed")

    # Brief pause to let server clean up
    time.sleep(1)

    # Second connection
    sse2 = SSEClient(f"http://{device_ip}/api/events")
    if not sse2.connect():
        results.check("SSE reconnect: second connect", True, False)
        return

    try:
        events2 = sse2.wait_for_events(2, timeout=settle_time + 5)
        results.check("SSE reconnect: second connection got events",
                      True, len(events2) >= 2)

        if len(events2) >= 2:
            results.check("SSE reconnect: first event is 'status'",
                          "status", events2[0][0])
            results.check("SSE reconnect: second event is 'lightstate'",
                          "lightstate", events2[1][0])
            print(f"  Second connection received {len(events2)} events")
    finally:
        sse2.close()


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
    parser.add_argument("--test", choices=["all", "onoff", "color", "brightness", "accuracy", "colortemp", "rgbw", "sse"],
                        default="all",
                        help="Which test to run (default: all)")

    args = parser.parse_args()

    # --- Discover bridge ---
    # SSE-only tests don't strictly need the Hue Bridge (except state change).
    bridge_ip = args.bridge_ip
    if not bridge_ip:
        bridge_ip = discover_bridge()
        if not bridge_ip:
            if args.test == "sse":
                print("WARNING: Could not discover Hue Bridge. "
                      "SSE state-change test will be skipped.")
            else:
                print("ERROR: Could not discover Hue Bridge. Use --bridge-ip.",
                      file=sys.stderr)
                sys.exit(1)
    if bridge_ip:
        print(f"Hue Bridge: {bridge_ip}")

    # --- API key ---
    api_key = args.api_key or load_api_key()
    if not api_key:
        if args.test == "sse":
            print("WARNING: No Hue API key. SSE state-change test will be skipped.")
        else:
            print("ERROR: No Hue API key found. Run tools/hue_debug.py first "
                  "to create one.", file=sys.stderr)
            sys.exit(1)
    if api_key:
        print(f"API key: {api_key[:8]}...")

    api = HueAPI(bridge_ip, api_key) if bridge_ip and api_key else None

    # --- Configure device and identify lights ---
    device_config = None
    device_eui64 = None
    light_ids = []

    if args.device_ip:
        # Get device status (EUI64, Zigbee state)
        print(f"\nQuerying device at {args.device_ip}...")
        device_status = get_device_status(args.device_ip)
        if device_status:
            device_eui64 = device_status.get("eui64")
            print(f"  Device EUI64: {device_eui64}")
            print(f"  Zigbee: {device_status.get('zigbee')}")

        # Ensure device is configured for ArtNet with test lights
        if not args.skip_artnet:
            device_config = configure_device_for_test(
                args.device_ip, universe=args.universe,
                listen_ip=args.listen_ip)
            if device_config is None:
                print("ERROR: Failed to configure device for testing.",
                      file=sys.stderr)
                sys.exit(1)
            args.universe = device_config.get("output", {}).get(
                "artnetUniverse", args.universe)
        else:
            # Just read the config without modifying it
            device_config = get_device_config(args.device_ip)

        if device_config:
            lights_cfg = device_config.get("lights", [])
            output_cfg = device_config.get("output", {})
            print(f"\n  Active config: {len(lights_cfg)} light(s), "
                  f"mode={output_cfg.get('mode', 'dmx')}")
            for i, lc in enumerate(lights_cfg):
                print(f"    [{i}] {lc.get('name')} type={lc.get('type')} "
                      f"dmxAddr={lc.get('dmxAddr')} (endpoint {10 + i * 10})")

    # Find matching Hue lights
    if api:
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
        elif args.test != "sse":
            print("  ERROR: No matching lights found on bridge.", file=sys.stderr)
            print("  Use --light-id to specify manually, or pair the device first.",
                  file=sys.stderr)
            sys.exit(1)

        if args.light_id:
            # Override with explicit light ID
            light_ids = [args.light_id]

    # --- Build DMX mapping from device config ---
    # Maps hue_light_id -> (dmx_start_addr, channel_map, light_type)
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
                    lc.get("channelMap", {"r": 0, "g": 1, "b": 2, "w": 3}),
                    lc.get("type", "RGB")
                )
            else:
                # Default mapping
                dmx_mappings[lid] = (1 + i * 3, {"r": 0, "g": 1, "b": 2}, "RGB")
    else:
        # No device config, assume default
        for i, lid in enumerate(light_ids):
            dmx_mappings[lid] = (1 + i * 3, {"r": 0, "g": 1, "b": 2}, "RGB")

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
                print("  ERROR: No ArtNet packets received within 10s.",
                      file=sys.stderr)
                print("  Is the device configured for ArtNet output?",
                      file=sys.stderr)
                print("  Use --skip-artnet to run without ArtNet verification.",
                      file=sys.stderr)
                listener.stop()
                sys.exit(1)

    # --- Run tests ---
    results = TestResult()

    for light_index, lid in enumerate(light_ids):
        if not api:
            break  # Hue bridge tests require api
        dmx_start, channel_map, light_type = dmx_mappings.get(
            lid, (1, {"r": 0, "g": 1, "b": 2}, "RGB"))
        print(f"\n{'=' * 60}")
        print(f"  TESTING LIGHT #{lid} (index={light_index}, type={light_type}, "
              f"DMX start={dmx_start}, map={channel_map})")
        print(f"{'=' * 60}")

        tests_to_run = args.test
        if tests_to_run in ("all", "onoff"):
            test_light_on_off(api, lid, listener, dmx_start, channel_map,
                              results, args.settle_time, light_type=light_type)
        if tests_to_run in ("all", "color"):
            test_light_color(api, lid, listener, dmx_start, channel_map,
                             results, args.settle_time)
        if tests_to_run in ("all", "brightness"):
            test_light_brightness(api, lid, listener, dmx_start, channel_map,
                                  results, args.settle_time,
                                  light_type=light_type)
        if tests_to_run in ("all", "accuracy"):
            test_light_color_accuracy(api, lid, listener, dmx_start, channel_map,
                                       results, args.settle_time,
                                       device_ip=args.device_ip,
                                       light_index=light_index)
        if tests_to_run in ("all", "colortemp"):
            test_light_color_temperature(api, lid, listener, dmx_start, channel_map,
                                          results, args.settle_time,
                                          light_type=light_type)
        if tests_to_run in ("all", "rgbw"):
            if light_type == "RGBW":
                test_rgbw_decomposition(api, lid, listener, dmx_start, channel_map,
                                         results, args.settle_time,
                                         device_ip=args.device_ip,
                                         light_index=light_index)
                test_rgbw_accuracy(api, lid, listener, dmx_start, channel_map,
                                    results, args.settle_time,
                                    device_ip=args.device_ip,
                                    light_index=light_index)
            elif tests_to_run == "rgbw":
                print(f"\n  SKIP: Light #{lid} is {light_type}, not RGBW")

        # Restore light to a neutral state
        print(f"\n  Restoring light #{lid} to white, ON, bri=254...")
        api.set_light_state(lid, {"on": True, "bri": 254, "xy": [0.3127, 0.3290]})

    # --- SSE tests (device-level, not per-light) ---
    tests_to_run = args.test
    if tests_to_run in ("all", "sse") and args.device_ip:
        print(f"\n{'=' * 60}")
        print(f"  SSE (Server-Sent Events) TESTS")
        print(f"{'=' * 60}")

        test_sse_initial_events(args.device_ip, results, args.settle_time)
        test_sse_concurrent_rest(args.device_ip, results, args.settle_time)
        test_sse_reconnect(args.device_ip, results, args.settle_time)

        # State change test needs Hue bridge + a light to toggle
        if light_ids and api:
            test_sse_state_change(args.device_ip, api, light_ids[0],
                                 results, args.settle_time)
        elif not api:
            print("\n  SKIP: SSE state-change test (no Hue bridge/API key)")

        # Keepalive test takes ~18s, run last
        test_sse_keepalive(args.device_ip, results)
    elif tests_to_run == "sse" and not args.device_ip:
        print("\n  SKIP: SSE tests require --device-ip")

    # --- Cleanup ---
    if listener:
        listener.stop()

    # --- Report ---
    all_passed = results.summary()
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()

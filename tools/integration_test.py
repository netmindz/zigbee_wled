#!/usr/bin/env python3
"""
Integration test for Zigbee WLED Bridge.

Verifies end-to-end that light states set via the Philips Hue REST API
are correctly reflected in the device's internal state (queried via the
ESP32 web API at /api/lights/state), which is what gets sent to WLED
devices over HTTP.

Test flow:
  1. Connect to the Hue Bridge (reuses .hue_api_key from hue_debug.py)
  2. Identify which Hue light ID(s) correspond to our ESP32 device
  3. Set a known color/brightness on the Hue light
  4. Query the ESP32's /api/lights/state to read the computed RGB/RGBW values
  5. Verify the device state matches the expected color conversion

Tests include:
  - ON/OFF: verify power state is reflected in device state
  - COLOR: verify CIE xy color changes produce correct RGB values
  - BRIGHTNESS: verify brightness scaling
  - ACCURACY: verify device-reported state matches expected color pipeline
  - COLORTEMP: verify mirek (color temperature) control
  - RGBW: verify white channel decomposition for RGBW-configured lights
    (W = min(R,G,B), then R'=R-W, G'=G-W, B'=B-W)
  - SSE: verify Server-Sent Events stream (no Hue Bridge required)

Prerequisites:
  pip install requests

Usage:
  # Auto-discover bridge, identify our device's light(s), run tests:
  python3 tools/integration_test.py --device-ip 192.168.178.110

  # Specify bridge IP and light ID explicitly:
  python3 tools/integration_test.py --bridge-ip 192.168.178.216 --light-id 25

  # Increase wait time for slow Zigbee propagation:
  python3 tools/integration_test.py --device-ip 192.168.178.110 --settle-time 3.0

  # Run only SSE tests (no Hue Bridge required):
  python3 tools/integration_test.py --device-ip 192.168.178.110 --test sse

  # Run only RGBW tests:
  python3 tools/integration_test.py --device-ip 192.168.178.110 --test rgbw
"""

import argparse
import json
import math
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

# Hue API key file (shared with hue_debug.py)
API_KEY_FILE = ".hue_api_key"

# Our device's manufacturer name as registered in Zigbee basic cluster
DEVICE_MANUFACTURER = "ZigbeeWLED"

# Tolerance for RGB value comparison (to account for float rounding)
RGB_TOLERANCE = 2

# Default settle time after sending a Hue command (seconds)
DEFAULT_SETTLE_TIME = 2.0


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


def get_device_status(device_ip: str) -> Optional[dict]:
    """Query the ESP32 device status API."""
    r = _device_request("GET", f"http://{device_ip}/api/status")
    if r is not None:
        return r.json()
    return None


def get_device_light_state(device_ip: str) -> Optional[list]:
    """Query the ESP32 device /api/lights/state for current per-light RGBW."""
    r = _device_request("GET", f"http://{device_ip}/api/lights/state")
    if r is not None:
        data = r.json()
        return data.get("lights", [])
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


def decompose_rgbw(r: int, g: int, b: int) -> tuple:
    """
    RGB-to-RGBW decomposition: extract the white component.
    Matches firmware's decomposeRGBW() in zigbee_manager.cpp.

    The white component is min(R, G, B), subtracted from each RGB channel.
    Returns (r', g', b', w).
    """
    w = min(r, g, b)
    return (r - w, g - w, b - w, w)


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


class TestResult:
    """Collects pass/fail results."""

    def __init__(self):
        self.tests = []
        self.passed = 0
        self.failed = 0

    def check(self, name: str, expected, actual, tolerance: int = RGB_TOLERANCE):
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
#  Test scenarios — Hue API -> device state verification
# ---------------------------------------------------------------------------

def test_light_on_off(api: HueAPI, light_id: str, device_ip: str,
                      light_index: int, results: TestResult,
                      settle_time: float, light_type: str = "RGB"):
    """Test turning a light on and off, verifying via device state API."""
    print(f"\n--- Test: ON/OFF for light #{light_id} ---")

    # Turn ON, full brightness, white
    print("  Setting light ON, brightness=254, white...")
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.3127, 0.3290]})
    time.sleep(settle_time)

    state = api.get_light(light_id).get("state", {})
    results.check(f"Light {light_id} ON: Hue reports on=True",
                  True, state.get("on"))

    # Verify device state
    dev_state = get_device_light_state(device_ip)
    if dev_state and light_index < len(dev_state):
        ls = dev_state[light_index]
        print(f"  Device state: on={ls.get('on')}, bri={ls.get('bri')}, "
              f"R={ls.get('r')} G={ls.get('g')} B={ls.get('b')} W={ls.get('w')}")
        results.check(f"Light {light_id} ON: device reports on=true",
                      True, ls.get("on"))

        if light_type == "RGBW":
            # For RGBW white, most output is on W channel
            results.check(f"Light {light_id} ON: RGBW W channel > 200",
                          True, ls.get("w", 0) > 200)
        else:
            # White at full brightness should have high values on all channels
            results.check(f"Light {light_id} ON: R > 200", True, ls.get("r", 0) > 200)
            results.check(f"Light {light_id} ON: G > 200", True, ls.get("g", 0) > 200)
            results.check(f"Light {light_id} ON: B > 200", True, ls.get("b", 0) > 200)
    else:
        results.check(f"Light {light_id} ON: device state readable", True, False)

    # Turn OFF
    print("  Setting light OFF...")
    api.set_light_state(light_id, {"on": False})
    time.sleep(settle_time)

    state = api.get_light(light_id).get("state", {})
    results.check(f"Light {light_id} OFF: Hue reports on=False",
                  False, state.get("on"))

    dev_state = get_device_light_state(device_ip)
    if dev_state and light_index < len(dev_state):
        ls = dev_state[light_index]
        print(f"  Device state: on={ls.get('on')}")
        results.check(f"Light {light_id} OFF: device reports on=false",
                      False, ls.get("on"))


def test_light_color(api: HueAPI, light_id: str, device_ip: str,
                     light_index: int, results: TestResult,
                     settle_time: float):
    """Test setting specific colors via CIE xy coordinates.

    NOTE: The Hue Bridge gamut-maps xy coordinates before sending ZCL
    commands, so the actual color the device receives may differ from
    what we requested.  We verify:
    1. The Bridge reports xy close to what we set
    2. The device state shows the intended channel is significant (>100)
    3. At least one channel is at full brightness (255)
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
                      xy[0], reported_xy[0], tolerance=0)
        results.check(f"Light {light_id} {color_name}: Hue xy[1] ~= {xy[1]:.3f}",
                      xy[1], reported_xy[1], tolerance=0)

        # Verify device state
        dev_state = get_device_light_state(device_ip)
        if dev_state and light_index < len(dev_state):
            ls = dev_state[light_index]
            dev_r, dev_g, dev_b = ls.get("r", 0), ls.get("g", 0), ls.get("b", 0)
            print(f"    Device state: R={dev_r} G={dev_g} B={dev_b}")

            channels = {"r": dev_r, "g": dev_g, "b": dev_b}

            # The intended channel should be significant (>100)
            results.check(
                f"Light {light_id} {color_name}: '{significant_ch}' channel > 100",
                True, channels[significant_ch] > 100)

            # At least one channel should be at full brightness (255)
            max_val = max(channels.values())
            results.check(
                f"Light {light_id} {color_name}: max channel == 255",
                True, max_val >= 250)
        else:
            results.check(f"Light {light_id} {color_name}: device state readable",
                          True, False)


def test_light_brightness(api: HueAPI, light_id: str, device_ip: str,
                          light_index: int, results: TestResult,
                          settle_time: float, light_type: str = "RGB"):
    """Test brightness scaling via the device's reported brightness."""
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

        # Verify device state reflects the brightness
        dev_state = get_device_light_state(device_ip)
        if dev_state and light_index < len(dev_state):
            ls = dev_state[light_index]
            dev_bri = ls.get("bri", 0)
            print(f"    Device: bri={dev_bri}, R={ls.get('r')} G={ls.get('g')} "
                  f"B={ls.get('b')} W={ls.get('w')}")
            results.check(f"Light {light_id} bri={bri}: device bri ~= {bri}",
                          bri, dev_bri, tolerance=1)
        else:
            results.check(f"Light {light_id} bri={bri}: device state readable",
                          True, False)


def test_light_color_accuracy(api: HueAPI, light_id: str, device_ip: str,
                              light_index: int, results: TestResult,
                              settle_time: float):
    """
    Test that the color pipeline from Hue API -> Zigbee -> device state
    produces colors consistent with the firmware's xyToRGB conversion.

    IMPORTANT: The Hue Bridge gamut-maps xy coordinates before sending ZCL
    commands, so the actual ZCL values may differ from what we requested.
    We query the device's own /api/lights/state to get the RGB the firmware
    actually computed, and compare against Bridge-reported xy.
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

        # Read the device's own computed RGB (what it received via ZCL)
        dev_state = get_device_light_state(device_ip)
        if not dev_state or light_index >= len(dev_state):
            results.check(f"Light {light_id} {name}: device state readable",
                          True, False)
            continue

        ls = dev_state[light_index]
        dev_r = ls.get("r", 0)
        dev_g = ls.get("g", 0)
        dev_b = ls.get("b", 0)
        print(f"    Device reports RGB: R={dev_r} G={dev_g} B={dev_b} "
              f"(on={ls.get('on')}, bri={ls.get('bri')})")

        # Compute expected from Bridge-reported xy (may differ from requested
        # due to gamut mapping, but should be in the same ballpark)
        state = api.get_light(light_id).get("state", {})
        reported_xy = state.get("xy", xy)
        exp_r, exp_g, exp_b = cie_xy_to_rgb(reported_xy[0], reported_xy[1])
        print(f"    Expected RGB (from Bridge xy={reported_xy}): "
              f"R={exp_r} G={exp_g} B={exp_b}")

        # Device RGB should be reasonably close to what xyToRGB produces
        # from the Bridge-reported xy (tolerance is wider because the Bridge
        # gamut-maps before sending ZCL, and ZCL precision is limited)
        results.check(f"Light {light_id} {name}: R accuracy",
                      exp_r, dev_r, tolerance=15)
        results.check(f"Light {light_id} {name}: G accuracy",
                      exp_g, dev_g, tolerance=15)
        results.check(f"Light {light_id} {name}: B accuracy",
                      exp_b, dev_b, tolerance=15)


def test_light_color_temperature(api: HueAPI, light_id: str, device_ip: str,
                                 light_index: int, results: TestResult,
                                 settle_time: float, light_type: str = "RGB"):
    """
    Test color temperature (mirek) control via the Hue API 'ct' parameter.
    Verifies that the firmware's mirekToRGB conversion produces expected values.
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

        # Check if the Hue API reported an error
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

        # Compute expected RGB from our Python port of the firmware
        exp_r, exp_g, exp_b = mirek_to_rgb(mirek)

        # Read device state
        dev_state = get_device_light_state(device_ip)
        if not dev_state or light_index >= len(dev_state):
            results.check(f"Light {light_id} CT {name}: device state readable",
                          True, False)
            continue

        ls = dev_state[light_index]

        if is_rgbw:
            dev_r = ls.get("r", 0)
            dev_g = ls.get("g", 0)
            dev_b = ls.get("b", 0)
            dev_w = ls.get("w", 0)

            # Apply decomposition to expected values
            exp_r2, exp_g2, exp_b2, exp_w = decompose_rgbw(exp_r, exp_g, exp_b)

            print(f"    Expected: R={exp_r2} G={exp_g2} B={exp_b2} W={exp_w}")
            print(f"    Device:   R={dev_r} G={dev_g} B={dev_b} W={dev_w}")

            results.check(f"Light {light_id} CT {name}: R accuracy",
                          exp_r2, dev_r, tolerance=10)
            results.check(f"Light {light_id} CT {name}: G accuracy",
                          exp_g2, dev_g, tolerance=10)
            results.check(f"Light {light_id} CT {name}: B accuracy",
                          exp_b2, dev_b, tolerance=10)
            results.check(f"Light {light_id} CT {name}: W accuracy",
                          exp_w, dev_w, tolerance=10)

            # Verify warm is more red-dominant, cool has high W
            if mirek >= 370:
                results.check(f"Light {light_id} CT {name}: warm has R > B",
                              True, dev_r >= dev_b)
            elif mirek <= 200:
                results.check(f"Light {light_id} CT {name}: cool has high W",
                              True, dev_w > 150)
        else:
            dev_r = ls.get("r", 0)
            dev_g = ls.get("g", 0)
            dev_b = ls.get("b", 0)

            print(f"    Expected: R={exp_r} G={exp_g} B={exp_b}")
            print(f"    Device:   R={dev_r} G={dev_g} B={dev_b}")

            results.check(f"Light {light_id} CT {name}: R accuracy",
                          exp_r, dev_r, tolerance=10)
            results.check(f"Light {light_id} CT {name}: G accuracy",
                          exp_g, dev_g, tolerance=10)
            results.check(f"Light {light_id} CT {name}: B accuracy",
                          exp_b, dev_b, tolerance=10)

            # Verify warm is more red, cool is more blue
            if mirek >= 370:
                results.check(f"Light {light_id} CT {name}: warm has R > B",
                              True, dev_r >= dev_b)
            elif mirek <= 200:
                results.check(f"Light {light_id} CT {name}: cool has B > 0",
                              True, dev_b > 50)


def test_rgbw_decomposition(api: HueAPI, light_id: str, device_ip: str,
                            light_index: int, results: TestResult,
                            settle_time: float):
    """
    Test RGB-to-RGBW white channel decomposition via device state.

    For RGBW lights, the firmware decomposes RGB into RGBW by extracting
    the white component: W = min(R,G,B), then R'=R-W, G'=G-W, B'=B-W.
    This test verifies the white channel is correctly populated.
    """
    print(f"\n--- Test: RGBW DECOMPOSITION for light #{light_id} ---")

    # Ensure light is on at full brightness
    api.set_light_state(light_id, {"on": True, "bri": 254})
    time.sleep(0.5)

    # ---- Test 1: Pure white -> all white channel, no RGB ----
    print("  Test 1: Pure white (xy=D65) -> W=max, RGB=0")
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.3127, 0.3290]})
    time.sleep(settle_time)

    dev_state = get_device_light_state(device_ip)
    if dev_state and light_index < len(dev_state):
        ls = dev_state[light_index]
        dev_r, dev_g, dev_b, dev_w = (ls.get("r", 0), ls.get("g", 0),
                                      ls.get("b", 0), ls.get("w", 0))
        print(f"    Device: R={dev_r} G={dev_g} B={dev_b} W={dev_w}")

        results.check(f"Light {light_id} RGBW white: W channel > 200",
                      True, dev_w > 200)
        results.check(f"Light {light_id} RGBW white: R channel < 30",
                      True, dev_r < 30)
        results.check(f"Light {light_id} RGBW white: G channel < 30",
                      True, dev_g < 30)
        results.check(f"Light {light_id} RGBW white: B channel < 30",
                      True, dev_b < 30)
    else:
        results.check(f"Light {light_id} RGBW white: device state readable",
                      True, False)

    # ---- Test 2: Saturated red -> no white channel ----
    print("  Test 2: Saturated red -> W=0, R=max")
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.6750, 0.3220]})
    time.sleep(settle_time)

    dev_state = get_device_light_state(device_ip)
    if dev_state and light_index < len(dev_state):
        ls = dev_state[light_index]
        dev_r, dev_w = ls.get("r", 0), ls.get("w", 0)
        print(f"    Device: R={dev_r} G={ls.get('g')} B={ls.get('b')} W={dev_w}")

        results.check(f"Light {light_id} RGBW red: R channel > 200",
                      True, dev_r > 200)
        results.check(f"Light {light_id} RGBW red: W channel < 30",
                      True, dev_w < 30)
    else:
        results.check(f"Light {light_id} RGBW red: device state readable",
                      True, False)

    # ---- Test 3: Saturated green -> no white channel ----
    print("  Test 3: Saturated green -> W low, G=max")
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.4091, 0.5180]})
    time.sleep(settle_time)

    dev_state = get_device_light_state(device_ip)
    if dev_state and light_index < len(dev_state):
        ls = dev_state[light_index]
        dev_g, dev_w = ls.get("g", 0), ls.get("w", 0)
        print(f"    Device: R={ls.get('r')} G={dev_g} B={ls.get('b')} W={dev_w}")

        results.check(f"Light {light_id} RGBW green: G channel > 200",
                      True, dev_g > 200)
        results.check(f"Light {light_id} RGBW green: W channel < 50",
                      True, dev_w < 50)
    else:
        results.check(f"Light {light_id} RGBW green: device state readable",
                      True, False)

    # ---- Test 4: Saturated blue -> no white channel ----
    print("  Test 4: Saturated blue -> W low, B=max")
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.1670, 0.0400]})
    time.sleep(settle_time)

    dev_state = get_device_light_state(device_ip)
    if dev_state and light_index < len(dev_state):
        ls = dev_state[light_index]
        dev_b, dev_w = ls.get("b", 0), ls.get("w", 0)
        print(f"    Device: R={ls.get('r')} G={ls.get('g')} B={dev_b} W={dev_w}")

        results.check(f"Light {light_id} RGBW blue: B channel > 200",
                      True, dev_b > 200)
        results.check(f"Light {light_id} RGBW blue: W channel < 30",
                      True, dev_w < 30)
    else:
        results.check(f"Light {light_id} RGBW blue: device state readable",
                      True, False)

    # ---- Test 5: Warm white via color temperature -> high W ----
    light_info = api.get_light(light_id)
    ct_cap = light_info.get("capabilities", {}).get("control", {}).get("ct")
    if ct_cap is not None:
        print("  Test 5: Warm white via CT=370 -> high W")
        api.set_light_state(light_id, {"on": True, "bri": 254, "ct": 370})
        time.sleep(settle_time)

        dev_state = get_device_light_state(device_ip)
        if dev_state and light_index < len(dev_state):
            ls = dev_state[light_index]
            dev_r, dev_g, dev_b, dev_w = (ls.get("r", 0), ls.get("g", 0),
                                          ls.get("b", 0), ls.get("w", 0))
            print(f"    Device: R={dev_r} G={dev_g} B={dev_b} W={dev_w}")

            results.check(f"Light {light_id} RGBW warm CT: W channel > 30",
                          True, dev_w > 30)
            results.check(f"Light {light_id} RGBW warm CT: R+W > G+W (warm tone)",
                          True, (dev_r + dev_w) >= (dev_g + dev_w))

        print("  Test 6: Cool daylight via CT=153 -> W present")
        api.set_light_state(light_id, {"on": True, "bri": 254, "ct": 153})
        time.sleep(settle_time)

        dev_state = get_device_light_state(device_ip)
        if dev_state and light_index < len(dev_state):
            ls = dev_state[light_index]
            dev_w = ls.get("w", 0)
            print(f"    Device: R={ls.get('r')} G={ls.get('g')} "
                  f"B={ls.get('b')} W={dev_w}")

            results.check(f"Light {light_id} RGBW cool CT: W channel > 150",
                          True, dev_w > 150)
    else:
        print("  SKIP: Tests 5-6 (CT not supported by bridge for this light)")

    # ---- Test 7: Light OFF -> all channels zero including W ----
    print("  Test 7: Light OFF -> all RGBW channels = 0")
    api.set_light_state(light_id, {"on": False})
    time.sleep(settle_time)

    dev_state = get_device_light_state(device_ip)
    if dev_state and light_index < len(dev_state):
        ls = dev_state[light_index]
        print(f"    Device: on={ls.get('on')}")
        results.check(f"Light {light_id} RGBW off: device reports on=false",
                      False, ls.get("on"))

    # ---- Test 8: Half brightness white -> W scaled, RGB low ----
    print("  Test 8: Half brightness white -> W scaled")
    api.set_light_state(light_id, {"on": True, "bri": 127, "xy": [0.3127, 0.3290]})
    time.sleep(settle_time)

    dev_state = get_device_light_state(device_ip)
    if dev_state and light_index < len(dev_state):
        ls = dev_state[light_index]
        dev_r, dev_w, dev_bri = ls.get("r", 0), ls.get("w", 0), ls.get("bri", 0)
        print(f"    Device: R={dev_r} G={ls.get('g')} B={ls.get('b')} "
              f"W={dev_w} bri={dev_bri}")

        # Brightness is reported separately — W should still be high
        # (the device stores full-intensity color, brightness applied at output)
        results.check(f"Light {light_id} RGBW half-bri: W channel > 200",
                      True, dev_w > 200)
        results.check(f"Light {light_id} RGBW half-bri: bri ~= 127",
                      127, dev_bri, tolerance=1)
        results.check(f"Light {light_id} RGBW half-bri: R channel < 30",
                      True, dev_r < 30)

    # Restore
    api.set_light_state(light_id, {"on": True, "bri": 254, "xy": [0.3127, 0.3290]})


def test_rgbw_accuracy(api: HueAPI, light_id: str, device_ip: str,
                       light_index: int, results: TestResult,
                       settle_time: float):
    """
    Test RGBW accuracy: verify device-reported RGBW values match
    expected decomposition for known colors.
    """
    print(f"\n--- Test: RGBW ACCURACY for light #{light_id} ---")

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
        dev_state = get_device_light_state(device_ip)
        if not dev_state or light_index >= len(dev_state):
            results.check(f"Light {light_id} RGBW {name}: device state readable",
                          True, False)
            continue

        ls = dev_state[light_index]
        dev_r = ls.get("r", 0)
        dev_g = ls.get("g", 0)
        dev_b = ls.get("b", 0)
        dev_w = ls.get("w", 0)
        print(f"    Device: R={dev_r} G={dev_g} B={dev_b} W={dev_w}")

        # Verify decomposition invariant: original RGB can be reconstructed
        # by adding W back to each channel
        orig_r = dev_r + dev_w
        orig_g = dev_g + dev_w
        orig_b = dev_b + dev_w
        print(f"    Reconstructed: R={orig_r} G={orig_g} B={orig_b}")

        # The reconstructed values should form a valid color
        # (at least one channel at 255 for full-brightness colors)
        max_val = max(orig_r, orig_g, orig_b)
        results.check(f"Light {light_id} RGBW {name}: reconstructed max >= 250",
                      True, max_val >= 250)

        # W should equal min of the reconstructed RGB (decomposition invariant)
        expected_w = min(orig_r, orig_g, orig_b)
        results.check(f"Light {light_id} RGBW {name}: W == min(R+W, G+W, B+W)",
                      expected_w, dev_w, tolerance=1)


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
            results.check("SSE initial: status is valid JSON", True, False)
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
        description="Integration test for Zigbee WLED Bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--bridge-ip",
                        help="Hue Bridge IP (auto-discovered if omitted)")
    parser.add_argument("--api-key",
                        help="Hue API key (loaded from .hue_api_key if omitted)")
    parser.add_argument("--device-ip",
                        help="ESP32 Zigbee WLED Bridge IP")
    parser.add_argument("--light-id",
                        help="Hue light ID to test (auto-detected if --device-ip given)")
    parser.add_argument("--settle-time", type=float, default=DEFAULT_SETTLE_TIME,
                        help=f"Seconds to wait after Hue command (default: {DEFAULT_SETTLE_TIME})")
    parser.add_argument("--test", choices=["all", "onoff", "color", "brightness",
                                           "accuracy", "colortemp", "rgbw", "sse"],
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

    # --- Query device ---
    device_config = None
    device_eui64 = None
    light_ids = []

    if args.device_ip:
        # Get device status (EUI64, Zigbee state)
        print(f"\nQuerying device at {args.device_ip}...")
        device_status = get_device_status(args.device_ip)
        if device_status:
            device_eui64 = device_status.get("eui64")
            zb_state = device_status.get("zigbee")
            print(f"  Device EUI64: {device_eui64}")
            print(f"  Zigbee: {zb_state}")

        # Read the device config
        device_config = get_device_config(args.device_ip)
        if device_config:
            lights_cfg = device_config.get("lights", [])
            print(f"\n  Active config: {len(lights_cfg)} light(s)")
            for i, lc in enumerate(lights_cfg):
                print(f"    [{i}] {lc.get('name')} type={lc.get('type')} "
                      f"host={lc.get('host')}:{lc.get('port')} "
                      f"(endpoint {10 + i * 10})")

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

    # --- Build light type mapping from device config ---
    light_types = {}
    if device_config:
        lights_cfg = device_config.get("lights", [])
        for i, lid in enumerate(light_ids):
            if i < len(lights_cfg):
                light_types[lid] = lights_cfg[i].get("type", "RGB")
            else:
                light_types[lid] = "RGB"
    else:
        for lid in light_ids:
            light_types[lid] = "RGB"

    # --- Run tests ---
    results = TestResult()

    for light_index, lid in enumerate(light_ids):
        if not api or not args.device_ip:
            break  # Hue bridge tests require api + device IP
        light_type = light_types.get(lid, "RGB")
        print(f"\n{'=' * 60}")
        print(f"  TESTING LIGHT #{lid} (index={light_index}, type={light_type})")
        print(f"{'=' * 60}")

        tests_to_run = args.test
        if tests_to_run in ("all", "onoff"):
            test_light_on_off(api, lid, args.device_ip, light_index,
                              results, args.settle_time, light_type=light_type)
        if tests_to_run in ("all", "color"):
            test_light_color(api, lid, args.device_ip, light_index,
                             results, args.settle_time)
        if tests_to_run in ("all", "brightness"):
            test_light_brightness(api, lid, args.device_ip, light_index,
                                  results, args.settle_time,
                                  light_type=light_type)
        if tests_to_run in ("all", "accuracy"):
            test_light_color_accuracy(api, lid, args.device_ip, light_index,
                                      results, args.settle_time)
        if tests_to_run in ("all", "colortemp"):
            test_light_color_temperature(api, lid, args.device_ip, light_index,
                                         results, args.settle_time,
                                         light_type=light_type)
        if tests_to_run in ("all", "rgbw"):
            if light_type == "RGBW":
                test_rgbw_decomposition(api, lid, args.device_ip, light_index,
                                        results, args.settle_time)
                test_rgbw_accuracy(api, lid, args.device_ip, light_index,
                                   results, args.settle_time)
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

    # --- Report ---
    all_passed = results.summary()
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()

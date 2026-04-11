#!/usr/bin/env python3
"""
Hue Bridge debug tool for Zigbee DMX Bridge development.

Queries the Philips Hue Bridge REST API to see what devices and lights
it has discovered.  Useful for debugging why the ESP32-C6 device pairs
at the Zigbee level but doesn't appear as a controllable light.

Features:
  - Auto-discovers the Hue Bridge via Philips NUPNP cloud lookup
  - Handles API key generation (link button press) and persists the key
    to a local file (.hue_api_key) which is git-ignored
  - Lists all lights, with Zigbee details (unique ID, model, manufacturer)
  - Shows new/unknown devices from the last light search
  - Can trigger a new Zigbee light search on the bridge
  - Optionally queries the ESP32 device status API for comparison

Prerequisites:
  pip install requests

Usage:
  # Auto-discover bridge, auto-load or create API key:
  python3 tools/hue_debug.py

  # Specify bridge IP explicitly:
  python3 tools/hue_debug.py --bridge-ip 192.168.178.216

  # Trigger a new light search:
  python3 tools/hue_debug.py --search

  # Also query the ESP32 device:
  python3 tools/hue_debug.py --device-ip 192.168.178.110

  # Show full details for a specific light:
  python3 tools/hue_debug.py --light-id 23

  # Show raw config/capabilities for all lights:
  python3 tools/hue_debug.py --raw
"""

import argparse
import json
import os
import sys
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

# File to persist the API key (relative to this script or CWD)
API_KEY_FILE = ".hue_api_key"
DEVICE_TYPE = "zigbee_dmx_debug"
NUPNP_URL = "https://discovery.meethue.com"


# ---------------------------------------------------------------------------
#  Hue Bridge Discovery
# ---------------------------------------------------------------------------

def discover_bridge_nupnp() -> Optional[str]:
    """Discover Hue Bridge IP via Philips NUPNP cloud service."""
    try:
        r = requests.get(NUPNP_URL, timeout=10)
        r.raise_for_status()
        bridges = r.json()
        if bridges:
            ip = bridges[0].get("internalipaddress")
            if ip:
                print(f"  Found bridge via NUPNP: {ip}")
                if len(bridges) > 1:
                    print(f"  (Note: {len(bridges)} bridges found, using first)")
                    for i, b in enumerate(bridges):
                        print(f"    [{i}] {b.get('internalipaddress', '?')} "
                              f"(id: {b.get('id', '?')})")
                return ip
    except Exception as e:
        print(f"  NUPNP discovery failed: {e}")
    return None


def discover_bridge_mdns() -> Optional[str]:
    """Try to discover Hue Bridge via mDNS (fallback)."""
    try:
        import socket
        # Simple approach: try to resolve _hue._tcp.local
        # This is a basic attempt; proper mDNS needs zeroconf library
        print("  mDNS discovery not implemented (install zeroconf for full support)")
    except Exception:
        pass
    return None


def discover_bridge() -> Optional[str]:
    """Try all discovery methods to find the Hue Bridge."""
    print("Discovering Hue Bridge...")
    ip = discover_bridge_nupnp()
    if ip:
        return ip
    ip = discover_bridge_mdns()
    if ip:
        return ip
    return None


# ---------------------------------------------------------------------------
#  API Key Management
# ---------------------------------------------------------------------------

def get_api_key_path() -> Path:
    """Return the path to the API key file (in project root)."""
    # Try project root first (where platformio.ini is)
    script_dir = Path(__file__).resolve().parent.parent
    pio_ini = script_dir / "platformio.ini"
    if pio_ini.exists():
        return script_dir / API_KEY_FILE
    # Fallback to CWD
    return Path.cwd() / API_KEY_FILE


def load_api_key() -> Optional[str]:
    """Load persisted API key from disk."""
    key_path = get_api_key_path()
    if key_path.exists():
        key = key_path.read_text().strip()
        if key:
            print(f"  Loaded API key from {key_path}")
            return key
    return None


def save_api_key(key: str):
    """Persist API key to disk."""
    key_path = get_api_key_path()
    key_path.write_text(key + "\n")
    print(f"  Saved API key to {key_path}")


def create_api_key(bridge_ip: str) -> str:
    """Create a new API key by pressing the link button on the bridge."""
    url = f"https://{bridge_ip}/api"

    print()
    print("=" * 60)
    print("  No API key found. Press the LINK BUTTON on your Hue Bridge")
    print("  then press Enter here within 30 seconds...")
    print("=" * 60)
    input("  Press Enter after pressing the link button... ")

    # Try a few times in case of timing
    for attempt in range(5):
        try:
            r = requests.post(url, json={"devicetype": DEVICE_TYPE},
                              verify=False, timeout=10)
            r.raise_for_status()
            result = r.json()
            if isinstance(result, list) and result:
                entry = result[0]
                if "success" in entry:
                    key = entry["success"]["username"]
                    print(f"  API key created: {key[:8]}...")
                    save_api_key(key)
                    return key
                elif "error" in entry:
                    err = entry["error"]
                    if err.get("type") == 101:
                        # Link button not pressed
                        if attempt < 4:
                            print(f"  Link button not pressed yet, retrying ({attempt+1}/5)...")
                            time.sleep(2)
                            continue
                    print(f"  Error: {err.get('description', err)}")
        except Exception as e:
            print(f"  Request failed: {e}")
            if attempt < 4:
                time.sleep(1)
                continue

    print("  Failed to create API key. Please try again.", file=sys.stderr)
    sys.exit(1)


def ensure_api_key(bridge_ip: str, provided_key: Optional[str] = None) -> str:
    """Get API key from args, file, or interactive creation."""
    if provided_key:
        # Verify it works
        try:
            r = requests.get(f"https://{bridge_ip}/api/{provided_key}/config",
                             verify=False, timeout=10)
            data = r.json()
            if isinstance(data, list) and data and "error" in data[0]:
                print(f"  WARNING: Provided API key may be invalid: "
                      f"{data[0]['error'].get('description')}")
            else:
                save_api_key(provided_key)
                return provided_key
        except Exception:
            pass
        return provided_key

    key = load_api_key()
    if key:
        # Verify it still works
        try:
            r = requests.get(f"https://{bridge_ip}/api/{key}/config",
                             verify=False, timeout=10)
            data = r.json()
            if isinstance(data, list) and data and "error" in data[0]:
                print(f"  Saved API key is invalid, will create new one")
                key = None
            else:
                return key
        except Exception:
            pass

    if not key:
        key = create_api_key(bridge_ip)

    return key


# ---------------------------------------------------------------------------
#  Hue API Client
# ---------------------------------------------------------------------------

class HueAPI:
    """Philips Hue Bridge REST API client."""

    def __init__(self, bridge_ip: str, api_key: str):
        self.bridge_ip = bridge_ip
        self.base_url = f"https://{bridge_ip}/api/{api_key}"
        self.verify = False
        self.timeout = 10

    def _get(self, path: str) -> dict:
        r = requests.get(f"{self.base_url}{path}",
                         verify=self.verify, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: Optional[dict] = None) -> list:
        r = requests.post(f"{self.base_url}{path}",
                          json=data or {}, verify=self.verify, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, data: dict) -> list:
        r = requests.put(f"{self.base_url}{path}",
                         json=data, verify=self.verify, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_config(self) -> dict:
        return self._get("/config")

    def get_lights(self) -> dict:
        return self._get("/lights")

    def get_light(self, light_id: str) -> dict:
        return self._get(f"/lights/{light_id}")

    def get_new_lights(self) -> dict:
        return self._get("/lights/new")

    def search_lights(self) -> list:
        """Trigger a new Zigbee light search (permit-join)."""
        return self._post("/lights")

    def get_groups(self) -> dict:
        return self._get("/groups")

    def get_sensors(self) -> dict:
        return self._get("/sensors")

    def get_resourcelinks(self) -> dict:
        return self._get("/resourcelinks")

    def set_light_state(self, light_id: str, state: dict) -> list:
        return self._put(f"/lights/{light_id}/state", state)

    def delete_light(self, light_id: str) -> list:
        r = requests.delete(f"{self.base_url}/lights/{light_id}",
                            verify=self.verify, timeout=self.timeout)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
#  Display Functions
# ---------------------------------------------------------------------------

def print_bridge_info(api: HueAPI):
    """Print bridge configuration summary."""
    cfg = api.get_config()
    print()
    print("=" * 60)
    print("  HUE BRIDGE INFO")
    print("=" * 60)
    print(f"  Name:        {cfg.get('name', '?')}")
    print(f"  Model:       {cfg.get('modelid', '?')}")
    print(f"  Bridge ID:   {cfg.get('bridgeid', '?')}")
    print(f"  SW Version:  {cfg.get('swversion', '?')}")
    print(f"  API Version: {cfg.get('apiversion', '?')}")
    print(f"  IP:          {cfg.get('ipaddress', '?')}")
    print(f"  Zigbee Ch:   {cfg.get('zigbeechannel', '?')}")
    print(f"  Link Button: {cfg.get('linkbutton', '?')}")
    wl = cfg.get("whitelist", {})
    print(f"  API Users:   {len(wl)}")


def print_lights_summary(api: HueAPI, raw: bool = False):
    """Print all lights with Zigbee details."""
    lights = api.get_lights()
    print()
    print("=" * 60)
    print(f"  ALL LIGHTS ({len(lights)} total)")
    print("=" * 60)

    if not lights:
        print("  No lights found on the bridge.")
        return

    for lid, light in sorted(lights.items(), key=lambda x: int(x[0])):
        state = light.get("state", {})
        reachable = state.get("reachable", False)
        on = state.get("on", False)
        bri = state.get("bri", "?")
        colormode = state.get("colormode", "?")

        status_icon = "ON " if on else "OFF"
        reach_icon = "OK" if reachable else "UNREACHABLE"

        print()
        print(f"  Light #{lid}: {light.get('name', '?')}")
        print(f"    Type:         {light.get('type', '?')}")
        print(f"    Model:        {light.get('modelid', '?')}")
        print(f"    Manufacturer: {light.get('manufacturername', '?')}")
        print(f"    Unique ID:    {light.get('uniqueid', '?')}")
        print(f"    SW Version:   {light.get('swversion', '?')}")
        print(f"    State:        {status_icon} | {reach_icon} | bri={bri} | mode={colormode}")

        if state.get("xy"):
            print(f"    CIE XY:       {state['xy']}")
        if state.get("hue") is not None:
            print(f"    Hue/Sat:      {state.get('hue')}/{state.get('sat')}")
        if state.get("ct") is not None:
            print(f"    Color Temp:   {state['ct']} mirek")

        # Parse endpoint from unique ID (format: xx:xx:xx:xx:xx:xx:xx:xx-NN)
        uid = light.get("uniqueid", "")
        if "-" in uid:
            ep_hex = uid.split("-")[-1]
            try:
                ep_num = int(ep_hex, 16)
                print(f"    Endpoint:     {ep_num} (0x{ep_hex})")
            except ValueError:
                pass

        cap = light.get("capabilities", {})
        if cap and raw:
            print(f"    Capabilities: {json.dumps(cap, indent=6)}")

        if raw:
            # Show everything
            print(f"    Full data:")
            for k, v in light.items():
                if k not in ("state", "capabilities", "name", "type",
                             "modelid", "manufacturername", "uniqueid", "swversion"):
                    print(f"      {k}: {json.dumps(v)}")


def print_endpoint_analysis(api: HueAPI, device_eui64: Optional[str] = None):
    """Analyze which endpoints the bridge has discovered for our device."""
    lights = api.get_lights()
    print()
    print("=" * 60)
    print("  ENDPOINT ANALYSIS (ZigbeeDMX devices)")
    print("=" * 60)

    # Group lights by IEEE address (from unique ID)
    devices = {}  # ieee_addr -> [(light_id, endpoint, name)]
    for lid, light in lights.items():
        uid = light.get("uniqueid", "")
        mfr = light.get("manufacturername", "")
        if mfr != "ZigbeeDMX" and not (device_eui64 and
            device_eui64.lower().replace(":", "") in uid.lower().replace(":", "").replace("-", "")):
            continue

        if "-" in uid:
            parts = uid.rsplit("-", 1)
            ieee_part = parts[0]
            try:
                ep_num = int(parts[1], 16)
            except ValueError:
                ep_num = -1
            devices.setdefault(ieee_part, []).append((lid, ep_num, light.get("name", "?")))

    if not devices:
        print("  No ZigbeeDMX devices found on bridge.")
        print("  Run: python3 tools/hue_debug.py --search")
        return

    for ieee, endpoints in devices.items():
        print(f"\n  Device IEEE: {ieee}")
        print(f"  Discovered endpoints ({len(endpoints)}):")
        for lid, ep, name in sorted(endpoints, key=lambda x: x[1]):
            print(f"    Endpoint {ep} (0x{ep:02x}) -> Light #{lid}: {name}")

        # Check for missing endpoints (based on expected range 10-25)
        discovered_eps = {ep for _, ep, _ in endpoints}
        min_ep = min(discovered_eps)
        max_ep = max(discovered_eps) if len(discovered_eps) > 1 else min_ep
        if max_ep == min_ep:
            print(f"\n  WARNING: Only 1 endpoint discovered (endpoint {min_ep}).")
            print(f"  If you have multiple lights configured, the Hue Bridge may not")
            print(f"  have discovered them all. This is a known Hue Bridge limitation.")
            print(f"  The bridge typically only discovers the first endpoint on a device.")
            print()
            print(f"  Workarounds:")
            print(f"    1. Try running --search again (sometimes repeated searches help)")
            print(f"    2. Delete this light, re-pair, and run search multiple times")
            print(f"    3. Use a coordinator that supports multi-endpoint devices:")
            print(f"       - deCONZ/Phoscon (Hue API compatible)")
            print(f"       - Zigbee2MQTT (with custom converter)")
            print(f"       - ZHA (Home Assistant)")
        else:
            print(f"  All expected endpoints appear to be discovered.")


def print_new_lights(api: HueAPI):
    """Print results of the last light search."""
    new = api.get_new_lights()
    print()
    print("=" * 60)
    print("  NEW / RECENTLY DISCOVERED LIGHTS")
    print("=" * 60)

    last_scan = new.pop("lastscan", "none")
    print(f"  Last scan: {last_scan}")

    if not new:
        print("  No new lights found in last search.")
    else:
        for lid, info in new.items():
            if isinstance(info, dict):
                print(f"  Light #{lid}: {info.get('name', '?')}")
            else:
                print(f"  Light #{lid}: {info}")


def print_sensors_summary(api: HueAPI):
    """Print ZGP and ZLL sensors (includes Zigbee devices)."""
    sensors = api.get_sensors()
    zigbee_sensors = {k: v for k, v in sensors.items()
                      if v.get("type", "").startswith("ZGP") or
                         v.get("type", "").startswith("ZLL") or
                         "uniqueid" in v}
    if not zigbee_sensors:
        return

    print()
    print("=" * 60)
    print(f"  ZIGBEE SENSORS / DEVICES ({len(zigbee_sensors)} with unique IDs)")
    print("=" * 60)
    for sid, sensor in sorted(zigbee_sensors.items(), key=lambda x: int(x[0])):
        print(f"  Sensor #{sid}: {sensor.get('name', '?')}")
        print(f"    Type:      {sensor.get('type', '?')}")
        print(f"    Model:     {sensor.get('modelid', '?')}")
        print(f"    Mfr:       {sensor.get('manufacturername', '?')}")
        print(f"    Unique ID: {sensor.get('uniqueid', '?')}")


def print_device_status(device_ip: str):
    """Query our ESP32 Zigbee DMX Bridge status API."""
    print()
    print("=" * 60)
    print(f"  ESP32 DEVICE STATUS ({device_ip})")
    print("=" * 60)

    try:
        r = requests.get(f"http://{device_ip}/api/status", timeout=5)
        r.raise_for_status()
        status = r.json()
        print(f"  WiFi IP:      {status.get('wifi', '?')}")
        print(f"  AP Mode:      {status.get('apMode', '?')}")
        print(f"  Zigbee Paired:{status.get('zigbee', '?')}")
        print(f"  EUI64:        {status.get('eui64', '?')}")
        print(f"  Light Count:  {status.get('lightCount', '?')}")
    except requests.exceptions.ConnectionError:
        print(f"  ERROR: Cannot connect to {device_ip}")
        print(f"  Device may be offline or on a different network")
    except Exception as e:
        print(f"  ERROR: {e}")

    try:
        r = requests.get(f"http://{device_ip}/api/config", timeout=5)
        r.raise_for_status()
        config = r.json()
        lights = config.get("lights", [])
        if lights:
            print(f"  Configured lights:")
            for i, light in enumerate(lights):
                print(f"    [{i}] {light.get('name', '?')} "
                      f"type={light.get('type', '?')} "
                      f"dmxAddr={light.get('dmxAddr', '?')} "
                      f"(endpoint {10 + i})")
        else:
            print(f"  No lights configured")
    except Exception:
        pass


def print_light_detail(api: HueAPI, light_id: str):
    """Print full detail for a single light."""
    try:
        light = api.get_light(light_id)
    except requests.exceptions.HTTPError as e:
        print(f"  ERROR: Light #{light_id} not found: {e}")
        return

    print()
    print("=" * 60)
    print(f"  LIGHT #{light_id} FULL DETAIL")
    print("=" * 60)
    print(json.dumps(light, indent=2))


# ---------------------------------------------------------------------------
#  Commands
# ---------------------------------------------------------------------------

def cmd_search(api: HueAPI):
    """Trigger a new light search on the bridge."""
    print()
    print("Triggering Zigbee light search on bridge...")
    result = api.search_lights()
    print(f"  Result: {json.dumps(result)}")
    print()
    print("  The bridge will search for ~60 seconds.")
    print("  Run this script again to see new lights.")
    print("  Make sure your ESP32 device is powered on and in pairing mode.")


def cmd_overview(api: HueAPI, device_ip: Optional[str], raw: bool):
    """Show full overview of bridge + device."""
    print_bridge_info(api)
    print_lights_summary(api, raw=raw)
    print_new_lights(api)
    print_sensors_summary(api)

    # Get device EUI64 for endpoint analysis
    eui64 = None
    if device_ip:
        try:
            r = requests.get(f"http://{device_ip}/api/status", timeout=5)
            eui64 = r.json().get("eui64")
        except Exception:
            pass
        print_device_status(device_ip)

    print_endpoint_analysis(api, eui64)


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Hue Bridge debug tool for Zigbee DMX Bridge development",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--bridge-ip",
                        help="Hue Bridge IP (auto-discovered if omitted)")
    parser.add_argument("--api-key",
                        help="Hue API key (loaded from .hue_api_key if omitted)")
    parser.add_argument("--device-ip",
                        help="ESP32 Zigbee DMX Bridge IP for status query")
    parser.add_argument("--search", action="store_true",
                        help="Trigger a new Zigbee light search on the bridge")
    parser.add_argument("--light-id",
                        help="Show full detail for a specific light ID")
    parser.add_argument("--raw", action="store_true",
                        help="Show raw/full data for all lights")
    parser.add_argument("--delete-light",
                        help="Delete a light from the bridge by ID")
    parser.add_argument("--endpoints", action="store_true",
                        help="Analyze which endpoints the bridge discovered for our device")

    args = parser.parse_args()

    # Discover bridge
    bridge_ip = args.bridge_ip
    if not bridge_ip:
        bridge_ip = discover_bridge()
        if not bridge_ip:
            print("ERROR: Could not discover Hue Bridge. "
                  "Use --bridge-ip to specify manually.", file=sys.stderr)
            sys.exit(1)

    print(f"Using bridge: {bridge_ip}")

    # Get API key
    api_key = ensure_api_key(bridge_ip, args.api_key)
    print(f"Using API key: {api_key[:8]}...")

    # Create client
    api = HueAPI(bridge_ip, api_key)

    # Execute requested command
    if args.delete_light:
        print(f"\nDeleting light #{args.delete_light}...")
        result = api.delete_light(args.delete_light)
        print(f"  Result: {json.dumps(result)}")
        return

    if args.search:
        cmd_search(api)
        return

    if args.endpoints:
        # Get device EUI64 if possible
        eui64 = None
        if args.device_ip:
            try:
                r = requests.get(f"http://{args.device_ip}/api/status", timeout=5)
                eui64 = r.json().get("eui64")
            except Exception:
                pass
        print_endpoint_analysis(api, eui64)
        return

    if args.light_id:
        print_light_detail(api, args.light_id)
        return

    # Default: show overview
    cmd_overview(api, args.device_ip, raw=args.raw)

    print()
    print("-" * 60)
    print("Tips:")
    print("  - If your ESP32 device doesn't appear as a light:")
    print("    1. Run: python3 tools/hue_debug.py --search")
    print("    2. Power-cycle the ESP32 device")
    print("    3. Try factory-resetting Zigbee on the ESP32 (POST /api/factory-reset)")
    print("    4. Check serial logs for Zigbee join/pairing errors")
    print("  - Use --device-ip to also query the ESP32 status API")
    print("  - Use --raw to see full light capabilities/config")
    print("  - Use --light-id N to inspect a specific light")


if __name__ == "__main__":
    main()

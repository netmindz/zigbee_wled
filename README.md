# Zigbee WLED Bridge

ESP32-C6 firmware that bridges Philips Hue (Zigbee) to [WLED](https://kno.wled.ge/) devices on the local network. Each configured WLED device appears as a separate Zigbee Extended Color Light on the Hue Bridge and is controlled via the WLED JSON API over HTTP.

```
Hue Bridge  ──(Zigbee ZCL)──>  ESP32-C6  ──(HTTP JSON API)──>  WLED devices
                                    │
                              Web config UI
                            (WLED discovery,
                             light definitions)
```

## Features

- **Hue-compatible Zigbee endpoints** — each configured WLED device appears as a separate Extended Color Light (device ID 0x010D) on the Philips Hue Bridge, with full multi-endpoint discovery support
- **WLED device discovery** — mDNS-based network scan finds WLED devices automatically; select from a list in the web UI
- **Up to 16 WLED lights** — each mapped to a WLED device by IP or hostname
- **Web configuration UI** — captive portal for initial WiFi setup, WLED device discovery, light management
- **Config persistence** — all settings stored in NVS (ESP32 Preferences), survive reboots
- **CIE XY and Hue/Saturation color support** — full color control from the Hue app
- **Color temperature support** — Hue CT (mirek) commands are converted to RGB for WLED output
- **RGBW white channel decomposition** — for WLED devices with RGBW LEDs, the white channel is automatically extracted from RGB values (`W = min(R, G, B)`) and sent as a 4-element color array
- **Change detection** — only sends HTTP updates when light state actually changes, minimizing network traffic
- **Proven Hue Bridge compatibility** — implements all critical pairing requirements discovered through extensive testing (End Device mode, ZLL distributed security, app_device_version=1, raw ZCL command handling)

## Hardware Requirements

- **ESP32-C6-DevKitC-1** (4MB or 8MB flash) — or any ESP32-C6 board with 802.15.4 radio
- **WLED devices** on the same local network — any ESP8266/ESP32 running WLED firmware

No additional hardware beyond the ESP32-C6 is needed. The bridge communicates with WLED devices over WiFi.

## Building

This is a PlatformIO project targeting the ESP32-C6 with Arduino framework and ESP-IDF v5.5.

```bash
# Install PlatformIO CLI (if not already installed)
pip install platformio

# Build (default 4MB flash)
pio run

# Build for 8MB flash variant
pio run -e esp32c6-8mb

# Build and upload via USB serial
pio run -t upload

# Monitor serial output
pio device monitor -b 115200
```

### Partition Table

The firmware uses custom partition layouts (`partitions/zigbee_dmx_4MB.csv` / `zigbee_dmx_8MB.csv`) that include:
- App partition(s) for the firmware
- NVS for configuration storage
- `zb_storage` and `zb_fct` partitions required by the Zigbee stack

## First-Time Setup

1. **Flash the firmware** to the ESP32-C6
2. **Connect to the captive portal** — the device starts a WiFi AP named `ZigbeeWLED-Setup` (open, no password)
3. **Configure WiFi** — enter your network SSID and password in the web UI. The device restarts and connects to your network.
4. **Access the web UI** — browse to the device's IP address (check serial output or your router's DHCP table)
5. **Add lights** — click "+ Add Light", use the "Scan" button to discover WLED devices on the network, select one, and save
6. **Pair with Hue Bridge** — open the Hue app, go to Settings > Lights > Search. The device should appear as a new light.

## Web UI

The web interface is accessible at `http://<device-ip>/` and provides:

- **Status bar** — WiFi connection, Zigbee pairing status, light count
- **Light configuration** — add, edit, delete lights mapped to WLED devices
- **WLED discovery** — scan the network for WLED devices via mDNS, showing device name, IP, LED count, RGB/RGBW type, and firmware version
- **OTA firmware update** — upload firmware binaries via the browser
- **Factory reset** — clears all configuration including WiFi credentials

### REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Device status (WiFi SSID, Zigbee state) |
| `/api/config` | GET | Full configuration (lights array) |
| `/api/config` | POST | Update configuration (JSON body) |
| `/api/lights/state` | GET | Current per-light RGB(W)/brightness state |
| `/api/wled/discover` | GET | Scan network for WLED devices via mDNS |
| `/api/wifi` | POST | Set WiFi credentials (triggers restart) |
| `/api/factory-reset` | POST | Erase all config and restart |
| `/api/restart` | POST | Restart the device |
| `/api/ota` | POST | Upload firmware binary (multipart form) |

## WLED Integration

### How It Works

The bridge sends HTTP POST requests to each configured WLED device's `/json/state` endpoint:

- **Set color:** `{"on":true,"bri":B,"seg":[{"fx":0,"col":[[R,G,B]]}]}`
- **Set RGBW color:** `{"on":true,"bri":B,"seg":[{"fx":0,"col":[[R,G,B,W]]}]}`
- **Turn off:** `{"on":false}`

The `fx:0` forces the "Solid" effect so color changes are immediate regardless of what effect was previously active on the WLED device.

### Brightness Mapping

Zigbee level control sends brightness as 0-254. WLED accepts 0-255. The bridge maps accordingly (254 -> 255, values 1-253 are incremented by 1).

### Rate Limiting

Updates are sent at most every 500ms (~2Hz). Additionally, the bridge tracks last-sent state per light and only sends HTTP requests when the state has actually changed. This avoids flooding WLED devices, which is especially important under WiFi/Zigbee coexistence where network capacity is limited.

### RGBW Support

If a light is configured as RGBW type, the bridge sends a 4-element color array `[R,G,B,W]` to the WLED device. The white channel is decomposed from the RGB values using `W = min(R, G, B)` with the remaining color subtracted from each channel.

### Device Discovery

The bridge uses mDNS to find WLED devices. WLED devices advertise as `_http._tcp` services with hostnames typically starting with `wled-`. For each discovered device, the bridge queries `/json/info` to retrieve:
- Device name
- LED count
- RGBW capability
- MAC address
- Firmware version

## Hue Bridge Pairing

The firmware implements several critical requirements for Philips Hue Bridge V2 compatibility:

1. **End Device mode** — Hue rejects Router joins to its distributed network
2. **Distributed security with ZLL link key** — the well-known ZLL distributed link key is required for Hue's network
3. **app_device_version = 1** — Hue requires this in the endpoint Simple Descriptor
4. **Extra on_off attributes** — global_scene_control, on_time, off_wait_time
5. **Minimum join LQI = 0** — PCB antennas often produce low LQI values
6. **Raw ZCL command handler** — required because:
   - Hue sends `off_with_effect` (0x40) instead of plain off, which ZBOSS silently ignores
   - Hue sends manufacturer-specific scene commands (0x100b) that crash ZBOSS
   - `SET_ATTR_VALUE_CB` doesn't fire for ZCL commands like `move_to_color`
7. **WiFi/802.15.4 coexistence** — enabled before WiFi starts, re-applied on WiFi reconnect

### Multi-Endpoint Support

Each configured light gets its own Zigbee endpoint with spaced numbering (endpoints 10, 20, 30, ...) and device ID `0x010D` (Extended Color Light). This matches the approach used by commercial multi-output Zigbee controllers and ensures the Hue Bridge discovers all endpoints as separate lights.

**After changing the number of lights**, you must:
1. Restart the device (endpoints are registered at Zigbee stack startup)
2. Delete the old light(s) from the Hue Bridge
3. Erase the Zigbee storage partition: `esptool.py erase_region 0x650000 0x4000`
4. Re-pair by running "Search for lights" in the Hue app

## Development Tools

### Hue Bridge Debug Tool

```bash
# Prerequisites
pip install requests

# Auto-discover bridge, show overview
python3 tools/hue_debug.py

# Specify bridge IP
python3 tools/hue_debug.py --bridge-ip 192.168.178.216

# Also query the ESP32 device status
python3 tools/hue_debug.py --device-ip 192.168.178.110

# Trigger a new light search
python3 tools/hue_debug.py --search

# Analyze discovered endpoints
python3 tools/hue_debug.py --endpoints

# Inspect a specific light
python3 tools/hue_debug.py --light-id 25 --raw

# Delete a light (for re-pairing)
python3 tools/hue_debug.py --delete-light 25
```

The API key is auto-generated on first use (requires pressing the Hue Bridge link button) and persisted to `.hue_api_key` (git-ignored).

## Project Structure

```
zigbee_wled/
├── platformio.ini              # PlatformIO configuration
├── partitions/
│   ├── zigbee_dmx_4MB.csv     # Custom 4MB partition table
│   └── zigbee_dmx_8MB.csv     # Custom 8MB partition table
├── include/
│   ├── config_store.h          # Configuration types and storage API
│   ├── wled_output.h           # WLED JSON API output
│   ├── wled_discovery.h        # mDNS WLED device discovery
│   ├── web_ui.h                # Web server and WiFi management
│   └── zigbee_manager.h        # Zigbee stack management
├── src/
│   ├── main.cpp                # Entry point, main loop
│   ├── config_store.cpp        # NVS persistence, JSON serialization
│   ├── wled_output.cpp         # HTTP POST to WLED /json/state
│   ├── wled_discovery.cpp      # mDNS scan + /json/info query
│   ├── web_ui.cpp              # Web UI, REST API, captive portal
│   └── zigbee_manager.cpp      # Zigbee endpoints, Hue compatibility
└── tools/
    ├── hue_debug.py            # Hue Bridge query/debug tool
    └── integration_test.py     # End-to-end verification test
```

## Configuration JSON Format

The configuration exchanged via `/api/config` follows this format:

```json
{
  "lights": [
    {
      "name": "Living Room Strip",
      "type": "RGB",
      "wledHost": "192.168.1.50",
      "wledPort": 80
    },
    {
      "name": "Bedroom Lamp",
      "type": "RGBW",
      "wledHost": "wled-abcdef.local",
      "wledPort": 80
    }
  ]
}
```

## Relationship to zigbee_dmx

This project is a fork of [zigbee_dmx](https://github.com/netmindz/zigbee_dmx), which bridges Hue to DMX512/ArtNet fixtures. The Zigbee stack, Hue pairing logic, color conversion, and RGBW decomposition are shared. The key difference is the output stage:

| | zigbee_dmx | zigbee_wled |
|---|---|---|
| **Output** | DMX512 (RS-485) or ArtNet (UDP) | WLED JSON API (HTTP) |
| **Hardware** | RS-485 transceiver + DMX fixtures | WiFi only + WLED devices |
| **Config per light** | DMX address + channel map | WLED host + port |
| **Discovery** | N/A (manual DMX addressing) | mDNS network scan |

## Known Issues

- **Zigbee reconfiguration requires restart** — changing the number of lights requires a device reboot and re-pair for Zigbee endpoints to be recreated (endpoints are registered at stack startup and cannot be dynamically changed).
- **WiFi/Zigbee coexistence** — the 2.4GHz WiFi and 802.15.4 Zigbee share the same radio band. Coexistence is enabled, but expect 1-2s ping latency and occasional packet loss. HTTP requests (including those to WLED devices) use generous timeouts (20s).
- **OTA unreliable** — firmware uploads over WiFi frequently fail due to coexistence. Use USB serial for flashing.
- **HTTP latency** — under heavy Zigbee traffic, HTTP requests to WLED devices may take several seconds. The 2Hz update rate and change-detection logic mitigate this.

## References

- [WLED JSON API Documentation](https://kno.wled.ge/interfaces/json-api/) — WLED state control and info endpoints
- [ESP-Zigbee SDK](https://github.com/espressif/esp-zigbee-sdk) — Espressif's Zigbee stack
- [WLED Zigbee RGB Light Usermod](https://github.com/netmindz/WLED-MM/tree/zigbee-rgb-light-usermod/usermods/zigbee_rgb_light/) — reference implementation this project was adapted from
- [zigbee_dmx](https://github.com/netmindz/zigbee_dmx) — parent project (DMX512/ArtNet output variant)
- [Zigbee Cluster Library (ZCL)](https://csa-iot.org/developer-resource/specifications-download-request/) — ZCL specification for color control, on/off, level control clusters

## License

This project is provided as-is for educational and development purposes.

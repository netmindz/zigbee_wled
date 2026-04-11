# Research Notes

Technical background, discoveries, and design decisions for the Zigbee WLED Bridge project. This document is intended for developers continuing work on the firmware. See `README.md` for usage instructions.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Hue Bridge Pairing Requirements](#hue-bridge-pairing-requirements)
- [Raw ZCL Command Handling](#raw-zcl-command-handling)
- [CIE XY Color Conversion](#cie-xy-color-conversion)
- [RGBW White Channel Decomposition](#rgbw-white-channel-decomposition)
- [WiFi / 802.15.4 Coexistence](#wifi--802154-coexistence)
- [Multi-Endpoint Discovery Problem](#multi-endpoint-discovery-problem)
- [ESP-Zigbee SDK Internals](#esp-zigbee-sdk-internals)
- [WLED JSON API](#wled-json-api)
- [WLED Device Discovery](#wled-device-discovery)
- [Build Environment and Library Issues](#build-environment-and-library-issues)
- [Runtime Bugs Found and Fixed](#runtime-bugs-found-and-fixed)
- [Reference Implementation](#reference-implementation)
- [Useful ESP-Zigbee SDK GitHub Issues](#useful-esp-zigbee-sdk-github-issues)
- [Future Work](#future-work)

---

## Architecture Overview

The firmware runs on an ESP32-C6 which has both a 2.4GHz WiFi radio and a native IEEE 802.15.4 radio (used for Zigbee). These share the same 2.4GHz band and require coexistence management.

```
                           ┌──────────────────────┐
                           │      ESP32-C6         │
                           │                       │
  Hue Bridge ──(802.15.4)──┤  Zigbee ZBOSS stack   │
                           │  (End Device mode)    │
                           │                       │
  Browser ────(WiFi/HTTP)──┤  WebServer + REST API │
                           │                       │
  WLED Devices ──(HTTP)────┤  HTTPClient POST to   │
                           │  /json/state endpoint │
                           └──────────────────────┘
```

**Threading model:**
- The Zigbee stack runs in its own FreeRTOS task (`zigbeeTask`, 16KB stack, priority 5). It calls `esp_zb_stack_main_loop()` which never returns.
- The Arduino `loop()` runs on the default task and handles the web server and WLED output at ~2Hz.
- Light state is shared between the Zigbee task and the main loop via `lightStates[]` protected by `zbStateMutex`.
- The WLED output loop reads the current light states, applies transition interpolation if active, and sends HTTP POST requests to each WLED device only when state has changed.

**Why not ESPHome?** ESPHome has no native Zigbee End Device support. The Hue-specific workarounds (ZLL distributed key, raw command handler for off_with_effect, crash prevention for manufacturer scene commands) require deep access to ZBOSS internals that ESPHome doesn't expose.

**Why End Device and not Router?** The Hue Bridge V2 operates a distributed-security Zigbee network. When a device joins as a Router, the bridge immediately sends a ZDO Leave command and rejects it. End Devices are accepted. This was discovered through packet captures during the WLED usermod development.

---

## Hue Bridge Pairing Requirements

These were discovered through extensive trial-and-error with the WLED Zigbee usermod and are all implemented in `src/zigbee_manager.cpp`. **Do not remove any of them** — each one caused pairing failures or runtime crashes when absent.

### 1. End Device Mode

```cpp
zb_nwk_cfg.esp_zb_role = ESP_ZB_DEVICE_TYPE_ED;
```

The Hue Bridge uses a distributed-security network (no centralized Trust Center). Router joins are rejected with ZDO Leave. End Devices are accepted.

Configuration:
- `ed_timeout = ESP_ZB_ED_AGING_TIMEOUT_64MIN` — the bridge will consider the device dead if it doesn't poll within 64 minutes
- `keep_alive = 3000` — keepalive interval in ms

### 2. Distributed Security with ZLL Link Key

```cpp
esp_zb_enable_joining_to_distributed(true);
esp_zb_secur_TC_standard_distributed_key_set(zll_distributed_key);
```

The well-known ZLL distributed link key is:
```
0x81, 0x42, 0x86, 0x86, 0x5D, 0xC1, 0xC8, 0xB2,
0xC8, 0xCB, 0xC5, 0x2E, 0x5D, 0x65, 0xD1, 0xB8
```

This key is published in the ZLL specification and is used by all ZLL-compatible devices. Without it, the device cannot decrypt the network key from the bridge during joining.

### 3. app_device_version = 1

```cpp
esp_zb_endpoint_config_t endpoint_config = {
    .endpoint = endpoint,
    .app_profile_id = ESP_ZB_AF_HA_PROFILE_ID,
    .app_device_id = ESP_ZB_HA_COLOR_DIMMABLE_LIGHT_DEVICE_ID,
    .app_device_version = 1,
};
```

The Hue Bridge checks `app_device_version` in the Simple Descriptor Response. Version 0 (the default in many examples) causes the bridge to ignore the device.

### 4. Extra On/Off Cluster Attributes

```cpp
esp_zb_on_off_cluster_add_attr(on_off_cluster, ESP_ZB_ZCL_ATTR_ON_OFF_GLOBAL_SCENE_CONTROL, &gsc);
esp_zb_on_off_cluster_add_attr(on_off_cluster, ESP_ZB_ZCL_ATTR_ON_OFF_ON_TIME, &on_time);
esp_zb_on_off_cluster_add_attr(on_off_cluster, ESP_ZB_ZCL_ATTR_ON_OFF_OFF_WAIT_TIME, &off_wait);
```

The Hue Bridge expects these attributes to be present in the on/off cluster. Without them, the bridge may not send on/off commands correctly, or may report the device as incompatible.

### 5. Minimum Join LQI = 0

```cpp
esp_zb_secur_network_min_join_lqi_set(0);
```

The ESP32-C6 PCB antenna has low gain. The default minimum LQI threshold (24) often exceeds what the PCB antenna achieves, causing the device to refuse to join networks it can actually communicate with.

### 6. RX On When Idle

```cpp
esp_zb_set_rx_on_when_idle(true);
```

Without this, the device sleeps and only wakes to poll. This causes unacceptable latency for light control commands (the Hue app expects sub-second response). With `rx_on_when_idle`, the radio stays on and responds immediately.

Combined with the long poll interval of 1000ms:
```cpp
zb_zdo_pim_set_long_poll_interval(1000);
zb_zdo_pim_toggle_turbo_poll_retry_feature(ZB_TRUE);
```

### 7. Color Capabilities

```cpp
light_cfg.color_cfg.color_capabilities = 0x0009;  // HS + XY
```

Bitmask: bit 0 = Hue/Saturation, bit 3 = XY. The Hue Bridge primarily uses CIE XY for color commands but also sends Hue/Saturation for some operations (e.g., color picker in the app).

### 8. Reportable Attribute Access Flags

```cpp
if (a) a->access |= ESP_ZB_ZCL_ATTR_ACCESS_REPORTING;
```

Attributes for on/off, level, color_x, color_y must have the reporting flag set. Without this, the bridge cannot configure attribute reporting and may consider the device non-functional.

### 9. ZCL String Format

Basic cluster string attributes (manufacturer name, model identifier, software build) use ZCL string format where the **first byte is the string length**:

```cpp
char manuf[] = "\x0B" "ZigbeeWLED";  // length 11 = 0x0B
char sw[] = "\x05" "0.1.0";          // length 5 = 0x05
```

Getting this wrong causes the bridge to display garbage or reject the device.

---

## Raw ZCL Command Handling

The `esp_zb_raw_command_handler_register(zb_raw_command_handler)` callback intercepts ZCL commands **before** the ZBOSS stack processes them. This is critical because:

### Problem 1: off_with_effect (ZBOSS Issue #519)

The Hue Bridge sends `off_with_effect` (command ID 0x40) on the On/Off cluster instead of a plain `off` (0x00) command. ZBOSS does not have a built-in handler for 0x40 and silently drops it. The light never turns off.

**Solution:** Intercept 0x40 in the raw handler, update the light state to off, manually set the on/off attribute, and send a default response with SUCCESS status.

### Problem 2: Manufacturer Scene Commands Crash (ZBOSS Issue #681)

The Hue Bridge periodically sends manufacturer-specific commands on the Scenes cluster with `manuf_specific = 0x100b`. ZBOSS attempts to parse these and hits a null pointer, crashing the device.

**Solution:** Intercept any Scenes cluster command with `is_manuf_specific && manuf_specific == 0x100b`, respond with FAIL status, and return true (consumed).

### Problem 3: SET_ATTR_VALUE_CB Doesn't Fire for ZCL Commands

The `ESP_ZB_CORE_SET_ATTR_VALUE_CB_ID` callback only fires when the ZBOSS stack internally processes a command and updates an attribute. For some commands like `move_to_color` (0x07), `move_to_hue_saturation` (0x06), `move_to_level` (0x00/0x04), ZBOSS updates the attribute but the callback may not fire, or fires before the attribute is fully updated.

**Solution:** Parse these commands in the raw handler, extract the payload values directly, and update the light state immediately. Return false to let ZBOSS also process the command (so the attribute cache stays in sync).

### Raw Handler Return Value

- `return true` — command is consumed, ZBOSS will not process it further
- `return false` — command continues to ZBOSS default processing

For off_with_effect and scene crash prevention, we return true. For move_to_color, move_to_hue_saturation, etc., we return false (we want ZBOSS to update the attribute cache, we just also need to capture the values).

### Payload Parsing

ZCL command payloads are accessed via:
```cpp
uint8_t *payload = (uint8_t *)zb_buf_begin(bufid);
zb_uint_t payload_len = zb_buf_len(bufid);
```

Payload formats (little-endian):
- **move_to_color (0x07):** `[colorX_lo, colorX_hi, colorY_lo, colorY_hi, transition_lo, transition_hi]` (6 bytes min)
- **move_to_hue_saturation (0x06):** `[hue, saturation, transition_lo, transition_hi]` (4 bytes min)
- **move_to_level (0x00):** `[level, transition_lo, transition_hi]` (3 bytes min)
- **move_to_level_with_on_off (0x04):** same format as 0x00 but also toggles on/off

### ZBOSS Internal Headers

The raw command handler requires ZBOSS internal headers not part of the public ESP-Zigbee SDK API:

```cpp
extern "C" {
    struct zb_af_endpoint_desc_s;
    typedef struct zb_af_endpoint_desc_s zb_af_endpoint_desc_t;
    #include "zboss_api_buf.h"
    #include "zcl/zb_zcl_common.h"
    // ... etc
}
```

The `extern "C"` block is required because these are C headers included from C++ code. The forward declaration of `zb_af_endpoint_desc_s` is needed because some headers reference it before defining it.

The undocumented functions used:
- `zb_zdo_pim_set_long_poll_interval()` — set ED polling interval
- `zb_zdo_pim_toggle_turbo_poll_retry_feature()` — enable turbo polling
- `zb_zcl_send_default_handler()` — send a ZCL default response to the coordinator

---

## CIE XY Color Conversion

The Hue Bridge uses CIE 1931 xy chromaticity coordinates for color control. The firmware converts between CIE xy and RGB.

### ZCL XY Encoding

ZCL encodes CIE xy as uint16 values scaled to 0-65279:
```
zcl_x = round(x * 65279)
zcl_y = round(y * 65279)
```

Where x, y are the CIE 1931 chromaticity coordinates (0.0 to 1.0 range, though actual values are bounded by the chromaticity diagram).

### XY to RGB Algorithm

1. Convert CIE xy to XYZ tristimulus (assuming Y=1.0 for maximum brightness):
   ```
   X = (1/y) * x
   Y = 1.0
   Z = (1/y) * (1 - x - y)
   ```

2. Apply the sRGB D65 inverse matrix (Wide RGB):
   ```
   R_linear =  3.2404542 * X - 1.5371385 * Y - 0.4985314 * Z
   G_linear = -0.9692660 * X + 1.8760108 * Y + 0.0415560 * Z
   B_linear =  0.0556434 * X - 0.2040259 * Y + 1.0572252 * Z
   ```

3. Clamp to [0, 1]

4. Apply reverse sRGB gamma:
   ```
   if (v <= 0.0031308) v_gamma = 12.92 * v
   else                v_gamma = 1.055 * v^(1/2.4) - 0.055
   ```

5. Scale so the maximum component equals 255:
   ```
   scale = 255 / max(R, G, B)
   R_out = round(R_gamma * scale)
   // etc.
   ```

This normalization step ensures the full dynamic range is used regardless of the color. Brightness is applied separately via the Zigbee level control (0-254).

### RGB to XY Algorithm (Reverse)

Used for `rgbToXY()` but not currently called in the firmware (it exists for potential future use like reporting state back to the coordinator):

1. Apply forward sRGB gamma:
   ```
   if (v/255 <= 0.04045) v_linear = (v/255) / 12.92
   else                  v_linear = ((v/255 + 0.055) / 1.055)^2.4
   ```

2. Apply sRGB D65 matrix to get XYZ
3. Convert XYZ to xy: `x = X/(X+Y+Z)`, `y = Y/(X+Y+Z)`

### Brightness Scaling

Brightness from Zigbee (level control cluster, 0-254) is applied at the WLED output stage via the WLED `bri` field, not in the color conversion. The WLED device handles brightness scaling internally — the bridge sends full-intensity RGB color values and a separate brightness value:

```cpp
doc["bri"] = wledBri;
seg0["col"][0] = [R, G, B];  // full intensity
```

This differs from the DMX variant where brightness was scaled into the RGB channel values. Using WLED's native brightness control produces smoother dimming since WLED applies its own gamma correction.

### Hue/Saturation to RGB

Some Hue app operations send Hue/Saturation instead of XY. The firmware converts HSV to RGB:
- ZCL Hue: 0-254 maps to 0-360 degrees
- ZCL Saturation: 0-254 maps to 0.0-1.0
- Value is always 1.0 (brightness handled separately)

Standard HSV-to-RGB conversion is used with six 60-degree segments.

---

## RGBW White Channel Decomposition

### Background

All lights are presented as RGB to the Zigbee/Hue Bridge regardless of whether the WLED device has RGB or RGBW LEDs. The Hue Bridge only sends RGB color data (via CIE XY, Hue/Saturation, or Color Temperature commands). For RGBW WLED devices, the firmware must extract the white component from the RGB values and send it as a separate white channel value.

### Algorithm

The decomposition uses a simple subtractive method:

```
W = min(R, G, B)
R' = R - W
G' = G - W
B' = B - W
```

This produces:
- **Pure white** (255, 255, 255) → R'=0, G'=0, B'=0, W=255 — fully on white channel
- **Warm white** (255, 180, 100) → R'=155, G'=80, B'=0, W=100 — partial white + color
- **Pure red** (255, 0, 0) → R'=255, G'=0, B'=0, W=0 — no white extraction
- **Light pink** (255, 200, 200) → R'=55, G'=0, B'=0, W=200 — mostly white + red tint

### Implementation

The `decomposeRGBW()` function is defined in `src/zigbee_manager.cpp` (~line 222):

```cpp
static void decomposeRGBW(uint8_t r, uint8_t g, uint8_t b,
                          uint8_t &rOut, uint8_t &gOut, uint8_t &bOut, uint8_t &wOut) {
    uint8_t w = min(r, min(g, b));
    rOut = r - w;
    gOut = g - w;
    bOut = b - w;
    wOut = w;
}
```

It is applied in **all 6 code paths** that set RGB values on a light:
1. `updateLightState()` — XY color updates from raw ZCL handler
2. `updateLightStateHS()` — Hue/Saturation updates
3. `updateLightStateCT()` — Color Temperature updates
4. `SET_ATTR_VALUE_CB` for XY — attribute write callback
5. `SET_ATTR_VALUE_CB` for CT — attribute write callback
6. Init defaults — initial state at startup

The decomposition is only applied for lights with `configStore.getLight(idx).type == LIGHT_TYPE_RGBW`. RGB lights pass through unchanged with W=0.

### WLED Color Array Format

For RGBW WLED devices, the decomposed values are sent as a 4-element array in the WLED JSON API:

```json
{"seg":[{"fx":0,"col":[[R', G', B', W]]}]}
```

For RGB devices, a 3-element array is used:

```json
{"seg":[{"fx":0,"col":[[R, G, B]]}]}
```

### Why Decompose at Color-Set Time?

The decomposition is applied when the color is received from Zigbee, not when WLED output is generated. This means the `LightState` struct always holds the decomposed values. Benefits:
- The web UI `/api/lights/state` endpoint can report the white channel value directly
- The WLED output code doesn't need to know about light types — it just outputs R, G, B, W from the state
- The live preview in the web UI can add W back into RGB for accurate color display

### Color Temperature and RGBW

Color temperature (CT) commands produce warm-to-cool whites that benefit significantly from RGBW decomposition. For example:
- **Warm white** (CT=454 mirek / 2200K) → RGB ≈ (255, 166, 87) → W=87, R'=168, G'=79, B'=0
- **Neutral white** (CT=333 mirek / 3000K) → RGB ≈ (255, 209, 163) → W=163, R'=92, G'=46, B'=0
- **Daylight** (CT=250 mirek / 4000K) → RGB ≈ (255, 232, 209) → W=209, R'=46, G'=23, B'=0
- **Cool white** (CT=153 mirek / 6500K) → RGB ≈ (255, 254, 250) → W=250, R'=5, G'=4, B'=0

Cool whites are almost entirely handled by the white channel, which produces much cleaner output on RGBW fixtures than mixing R+G+B to approximate white.

---

## WiFi / 802.15.4 Coexistence

The ESP32-C6 has a single 2.4GHz radio shared between WiFi and 802.15.4 (Zigbee). The coexistence mechanism time-multiplexes access.

### Setup

Coexistence must be enabled **before WiFi starts**:

```cpp
esp_coex_wifi_i154_enable();
```

Priority is set to MIDDLE for Zigbee, IDLE for idle state:

```cpp
esp_ieee802154_coex_config_t coex_cfg = {
    .idle    = IEEE802154_IDLE,
    .txrx    = IEEE802154_MIDDLE,
    .txrx_at = IEEE802154_MIDDLE,
};
esp_ieee802154_set_coex_config(coex_cfg);
```

### Re-application on WiFi Connect

Coexistence settings must be re-applied after WiFi connects. The `zigbeeStart()` function (called from `webOnWifiConnected()`) re-enables coexistence.

### Preprocessor Guards

```cpp
#if defined(CONFIG_ESP_COEX_SW_COEXIST_ENABLE) || defined(CONFIG_ESP_COEX_ENABLED)
```

These guards ensure the coexistence code only compiles when the SDK is configured for it. The pioarduino platform used in this project has it enabled by default.

### Performance Impact

WiFi traffic can delay Zigbee responses. The WLED output rate is set to 2Hz (500ms interval) to avoid overwhelming the WiFi stack under 802.15.4 coexistence. HTTP requests to WLED devices use a 20-second timeout to tolerate the 1-2 second ping latency and 30-50% packet loss that occur during coexistence. The change-detection logic in `WledOutput::stateChanged()` ensures that HTTP requests are only sent when the light state actually differs from the last successfully sent state, minimizing unnecessary WiFi traffic.

---

## Multi-Endpoint Discovery Problem (SOLVED)

### The Issue

The Hue Bridge V2 initially only discovered the **first light endpoint** on our multi-endpoint device. Our device registered endpoints 10, 11, 12, etc. (one per configured light), but the bridge only created a Hue light resource for endpoint 10.

### Root Cause

The Hue Bridge was designed for single-endpoint bulbs (all official Hue products are single-endpoint). With sequential endpoint numbering (10, 11, 12...) and device ID `0x0102` (Color Dimmable Light), the bridge's discovery logic:

1. Device joins the network
2. Bridge sends Active Endpoints Request (ZDO 0x0005)
3. Device responds with all endpoints: `[10, 11]`
4. Bridge sends Simple Descriptor Request for endpoint 10
5. Bridge creates a light resource for endpoint 10
6. **Bridge stops.** It does not send Simple Descriptor Request for endpoint 11.

### Solution

Research into commercial multi-endpoint Zigbee controllers (Gledopto 2ID, Dresden Elektronik FLS-PP lp) revealed two key differences:

1. **Device ID `0x010D`** (Extended Color Light) instead of `0x0102` (Color Dimmable Light) — the ESP-Zigbee SDK doesn't define `0x010D` as a constant, so the raw value is used directly in the endpoint config
2. **Widely spaced endpoint numbers** (10, 20, 30...) instead of sequential (10, 11, 12...) — controlled by `ZIGBEE_ENDPOINT_SPACING=10`

Both changes together fixed the issue. After re-pairing, the Hue Bridge now discovers all endpoints as separate lights. Confirmed working with 2 endpoints — both appear as "Extended color light" in the Hue app with full color, brightness, and color temperature control.

The `endpointToIndex()` reverse mapping handles the spaced numbering via modulo check and division.

### Evidence

- Initial attempt (device ID `0x0102`, sequential endpoints): endpoint 10 discovered as Light #25, endpoint 11 never appeared
- After fix (device ID `0x010D`, spaced endpoints 10/20): both discovered as Light #26 and Light #27
- Integration test: 132/132 tests pass across both lights (ON/OFF, color, brightness, accuracy, color temperature)
- ESP-Zigbee SDK Issue #598: same problem with Amazon Alexa (Echo Plus)
- ESP-Zigbee SDK Issue #407: Zigbee2MQTT needed a device converter but does discover all endpoints

### Known ESP-Zigbee SDK Multi-Endpoint Bugs

- **Issue #528**: Group commands (multicast) to color control only reach one endpoint
- **Issue #295**: Commanding one endpoint can block commands to another
- **Issue #656**: More than ~20 endpoints causes ZBOSS assertion failures

---

## ESP-Zigbee SDK Internals

### Platform Configuration

```cpp
esp_zb_platform_config_t platform_cfg = {};
platform_cfg.radio_config.radio_mode = ZB_RADIO_MODE_NATIVE;
platform_cfg.host_config.host_connection_mode = ZB_HOST_CONNECTION_MODE_NONE;
```

`ZB_RADIO_MODE_NATIVE` uses the ESP32-C6's built-in 802.15.4 radio. `ZB_HOST_CONNECTION_MODE_NONE` means the Zigbee stack runs on the same chip (no external Zigbee co-processor).

### Buffer and Queue Sizes

```cpp
esp_zb_io_buffer_size_set(128);
esp_zb_scheduler_queue_size_set(128);
```

These control ZBOSS internal memory pools. 128 is sufficient for a few endpoints. Increasing beyond ~200 may cause memory allocation failures on the ESP32-C6 (320KB RAM).

### Channel Mask

```cpp
esp_zb_set_primary_network_channel_set(ESP_ZB_TRANSCEIVER_ALL_CHANNELS_MASK);
```

Allows joining on any Zigbee channel (11-26). The Hue Bridge typically operates on channel 11, 15, 20, or 25.

### Attribute Cache

The ZBOSS stack maintains an attribute cache for each endpoint. Attributes can be read with:

```cpp
esp_zb_zcl_attr_t *attr = esp_zb_zcl_get_attribute(endpoint, cluster_id, role, attr_id);
if (attr && attr->data_p) {
    uint8_t value = *(uint8_t *)attr->data_p;
}
```

The raw command handler reads from this cache to get the current state when processing partial updates (e.g., a color change without an accompanying on/off change).

### Endpoint Registration

Endpoints must be registered before `esp_zb_start()`. They cannot be added or removed dynamically after the stack starts. Changing the light configuration therefore requires a device reboot.

```cpp
esp_zb_ep_list_t *ep_list = esp_zb_ep_list_create();
// ... add endpoints ...
esp_zb_device_register(ep_list);
// ... then start the stack:
esp_zb_start(false);
```

### sdkconfig.h Guard

The Zigbee code is guarded with:
```cpp
#include "sdkconfig.h"
#if defined(CONFIG_IDF_TARGET_ESP32C6) || ...
```

The `#include "sdkconfig.h"` is **mandatory** before the `#if`. Without it, the preprocessor guard runs before any SDK headers are loaded and `CONFIG_IDF_TARGET_ESP32C6` is undefined, so the entire Zigbee implementation is silently excluded.

---

## WLED JSON API

### Overview

WLED exposes a JSON-based HTTP API for controlling LED state. The bridge uses two endpoints:
- **`/json/state`** (POST) — set the LED state (color, brightness, on/off, effect)
- **`/json/info`** (GET) — query device metadata (name, LED count, RGBW capability, firmware version)

Full API documentation: https://kno.wled.ge/interfaces/json-api/

### State Control Payloads

**Turn on with color (RGB):**
```json
{"on":true,"bri":128,"seg":[{"fx":0,"col":[[255,0,0]]}]}
```

**Turn on with color (RGBW):**
```json
{"on":true,"bri":255,"seg":[{"fx":0,"col":[[155,80,0,100]]}]}
```

**Turn off:**
```json
{"on":false}
```

### Key Fields

| Field | Type | Description |
|-------|------|-------------|
| `on` | bool | Power state |
| `bri` | uint8 | Master brightness (0-255) |
| `seg` | array | Segment array — we always target segment 0 |
| `seg[0].fx` | uint8 | Effect ID — 0 = "Solid" (overrides any active animation) |
| `seg[0].col` | array | Color slots — `col[0]` is the primary color |
| `seg[0].col[0]` | array | RGB `[R,G,B]` or RGBW `[R,G,B,W]` values (0-255 each) |

### Why fx:0 is Required

WLED devices may have an active effect (e.g., rainbow, breathing, etc.) from manual or app-based control. Setting `fx:0` forces the "Solid" effect, ensuring the bridge's color command takes immediate visual effect. Without this, the color values might be ignored or modulated by the running effect.

### Brightness Mapping

Zigbee level control uses 0-254 range. WLED uses 0-255. The mapping in `src/wled_output.cpp`:

```cpp
uint8_t wledBri = (state.brightness >= 254) ? 255
                : static_cast<uint8_t>(state.brightness + (state.brightness > 0 ? 1 : 0));
```

This maps: 0→0, 1→2, 2→3, ..., 253→254, 254→255. The +1 offset avoids the Zigbee value 1 mapping to WLED value 1 (which is extremely dim and indistinguishable from off on most LED strips).

### /json/info Response Format

The discovery process queries `/json/info` to get device metadata:

```json
{
  "ver": "0.14.0",
  "name": "WLED-Living-Room",
  "mac": "AA:BB:CC:DD:EE:FF",
  "leds": {
    "count": 60,
    "rgbw": true,
    "wv": true
  }
}
```

Relevant fields used by discovery:
- `name` — display name shown in the web UI discovery panel
- `mac` — used for device identification
- `ver` — firmware version, displayed in the UI
- `leds.count` — number of LEDs, shown in the discovery card
- `leds.rgbw` — whether the device supports RGBW (determines 3 or 4 element color array)

### HTTP Configuration

```cpp
static const int HTTP_TIMEOUT_MS = 20000;  // 20 second timeout
```

The generous timeout accounts for WiFi/Zigbee coexistence delays. Under coexistence, WiFi round-trip times of 1-2 seconds are typical, with occasional spikes much higher. The `HTTPClient` library from the Arduino ESP32 framework handles connection pooling automatically — no additional `lib_deps` entry is needed in `platformio.ini`.

### Change Detection

The `WledOutput` class tracks the last successfully sent state per light in a `SentState` struct:

```cpp
struct SentState {
    bool valid;       // has any state been sent?
    bool powerOn;
    uint8_t brightness;
    uint8_t red, green, blue, white;
};
```

Before sending, `stateChanged()` compares the current light state against the last-sent state. If nothing changed, the HTTP request is skipped entirely. On send failure, the `valid` flag is cleared so the next cycle retries.

### Error Tracking

Consecutive send failures are tracked per device. Error logging uses a power-of-2 strategy (`(errors & (errors - 1)) == 0`) to avoid flooding the serial console — errors are logged at 1, 2, 4, 8, 16, 32, ... consecutive failures. When a device recovers, a recovery message is logged with the error count.

---

## WLED Device Discovery

### mDNS Scanning

WLED devices advertise themselves as `_http._tcp` mDNS services. The discovery process in `src/wled_discovery.cpp`:

1. Initialize mDNS with hostname `zigbeewled` (retries once on failure)
2. Query for `_http._tcp` services using `MDNS.queryService("http", "tcp")`
3. Filter results by hostname — only devices with hostnames starting with `wled` (case-insensitive) are probed
4. For each candidate, query `http://<ip>:<port>/json/info` via HTTP GET
5. Parse the JSON response to extract device metadata
6. Return the list of discovered `WledDeviceInfo` structs

### ESP32 mDNS API Notes

The ESP32 Arduino mDNS library has a different API from some other Arduino mDNS implementations:
- Use `MDNS.address(i)` to get the IP address (not `MDNS.IP(i)` — this was a build error we encountered)
- `MDNS.hostname(i)` returns the hostname
- `MDNS.port(i)` returns the port number

### Hostname Filtering

The discovery only probes devices whose mDNS hostname starts with `wled`. This is a performance optimization — without filtering, every HTTP service on the network would receive an HTTP GET request, which is slow under coexistence conditions (10-second timeout per device). WLED devices use default hostnames like `wled-AABBCC` (based on MAC address). Devices with custom hostnames that don't start with `wled` will not be discovered.

### Discovery Timeout

The mDNS query itself is fast (~2 seconds), but probing each discovered device via HTTP is slow under coexistence. With a 10-second per-device timeout, scanning a network with many HTTP services could take a long time. The hostname filter reduces this to only WLED devices.

### Web UI Integration

The discovery endpoint `/api/wled/discover` is called when the user clicks "Scan" in the web UI. The response is a JSON array of discovered devices:

```json
[
  {
    "name": "WLED-Living-Room",
    "host": "192.168.1.50",
    "port": 80,
    "mac": "AA:BB:CC:DD:EE:FF",
    "version": "0.14.0",
    "ledCount": 60,
    "isRGBW": true
  }
]
```

Each device is shown as a selectable card in the UI. Clicking a device populates the light configuration form with the device's IP and port.

---

## Build Environment and Library Issues

### Platform

We use the pioarduino fork of the Espressif32 PlatformIO platform:
```
platform = https://github.com/pioarduino/platform-espressif32/releases/download/55.03.37/platform-espressif32.zip
```

This provides:
- Arduino framework v3.3.7
- ESP-IDF v5.5.0
- ESP-Zigbee SDK (included in the libs package)
- RISC-V GCC 14.2.0

### ESPAsyncWebServer Incompatibility

ESPAsyncWebServer + AsyncTCP is incompatible with the ESP32-C6 USB serial configuration. The C6 uses a hardware UART over USB (not USB CDC like the S3), and `ARDUINO_USB_CDC_ON_BOOT=1` causes compilation failures in AsyncTCP. We use the built-in synchronous `WebServer` library instead.

### ArduinoJson v7

ArduinoJson v7.4.3 changed the `is<JsonArray>()` API. `doc["lights"].is<JsonArray>()` returns false even when the key contains an array. The fix is to use `doc["lights"].as<JsonArrayConst>()` and check `.isNull()` instead.

### HTTPClient Library

The Arduino ESP32 framework includes `HTTPClient` out of the box — no `lib_deps` entry is needed. This was discovered during the zigbee_wled transformation. The library provides:
- Connection timeout and read timeout configuration
- HTTP POST with custom headers (Content-Type: application/json)
- Automatic connection handling (no keep-alive pool management needed)

### ESP32 mDNS Library

The `ESPmDNS` library is also built into the Arduino ESP32 framework. Key discovery: the IP accessor method is `MDNS.address(i)`, not `MDNS.IP(i)` — this caused a build error during initial development.

### Build Size

The WLED variant builds successfully on the 4MB partition layout:
- **RAM usage:** 20.1%
- **Flash usage:** 90.9%

Flash is near capacity. Adding significant new features may require switching to the 8MB flash variant or optimizing the web UI HTML (which is compiled into the binary as string literals).

---

## Runtime Bugs Found and Fixed

### NVS First-Boot Failure

`Preferences.begin("namespace", true)` (read-only) fails with `NOT_FOUND` if the namespace has never been written. Changed to `Preferences.begin("namespace", false)` (read-write) which creates the namespace if it doesn't exist.

### ARDUINO_USB_CDC_ON_BOOT Flag

The `ARDUINO_USB_CDC_ON_BOOT=1` build flag is incorrect for ESP32-C6. The C6 uses `HWCDC` (hardware UART over USB), not the USB CDC class used by the S3. This flag was removed from `platformio.ini`.

### sdkconfig.h Include Order

The preprocessor guard `#if defined(CONFIG_IDF_TARGET_ESP32C6)` must appear **after** `#include "sdkconfig.h"`. Without the include, the macro is undefined and the entire Zigbee implementation is silently excluded from compilation.

### ESP32 mDNS API Mismatch

The initial code used `MDNS.IP(i)` to retrieve discovered device IP addresses, based on other Arduino mDNS library documentation. The ESP32 Arduino `ESPmDNS` library uses `MDNS.address(i)` instead. This caused a compilation error that was fixed during the zigbee_wled build.

---

## Reference Implementation

This project is adapted from the WLED Zigbee RGB Light usermod:
- **Repository:** https://github.com/netmindz/WLED-MM/tree/zigbee-rgb-light-usermod/usermods/zigbee_rgb_light/
- **Key files:** `usermod_zigbee_rgb_light.h` (single file, ~800 lines)
- **Differences:** The usermod runs inside WLED and controls LED strips directly. This project is standalone — it acts as a Zigbee-to-HTTP bridge, receiving Zigbee commands from the Hue Bridge and forwarding them to external WLED devices over the network.
- **All Hue-specific workarounds** in this project originated from that usermod and were proven through months of testing with a Hue Bridge V2.

### Relationship to zigbee_dmx

This project is a fork of [zigbee_dmx](https://github.com/netmindz/zigbee_dmx). The shared code includes:
- Zigbee stack initialization and Hue pairing logic (`zigbee_manager.cpp`)
- CIE XY and HSV color conversion
- RGBW white channel decomposition
- Web UI framework (WiFi setup, captive portal, REST API structure)
- Configuration persistence via NVS

The output stage was replaced: DMX512/ArtNet UART and UDP output → HTTP POST to WLED JSON API. The configuration model was simplified: DMX address + channel map → WLED host + port. A new mDNS discovery system was added.

The `upstream` remote in the git repo points to the local zigbee_dmx repository for cherry-picking shared fixes (e.g., Zigbee stack improvements, color conversion bug fixes).

---

## Useful ESP-Zigbee SDK GitHub Issues

| Issue | Title | Relevance |
|-------|-------|-----------|
| #519 | off_with_effect not handled by ZBOSS | Why we need the raw command handler for 0x40 |
| #681 | Hue manufacturer scene commands crash | Why we intercept manuf_specific 0x100b |
| #598 | Multiple endpoints not discovered by Alexa | Confirms multi-endpoint is a coordinator issue |
| #407 | Multiple endpoints in Z2M | Z2M needs custom converter for multi-endpoint |
| #295 | Odd behavior with multiple on/off endpoints | Inter-endpoint command blocking |
| #528 | Group commands only reach one endpoint | ZBOSS bug with multicast + multi-endpoint |
| #656 | >20 endpoints causes assertion failure | ZBOSS buffer pool limit |

All issues at: https://github.com/espressif/esp-zigbee-sdk/issues/

---

## Future Work

### High Priority

- **Zigbee state reporting** — implement `zigbeeReportState()` to send attribute reports back to the coordinator when state changes from the web UI
- **WLED state sync** — periodically query WLED `/json/state` to detect out-of-band changes (e.g., user controlling WLED via its own app) and report them back to Hue

### Medium Priority

- **mDNS service advertisement** — advertise the bridge's web UI via mDNS so it's discoverable without knowing the IP
- **Web UI improvements** — light count change should trigger immediate Zigbee reconfigure without requiring manual restart
- **WLED segments** — support targeting specific WLED segments instead of always controlling segment 0, allowing one WLED device to present multiple Zigbee lights for different strip sections
- **Transition animations** — send WLED's native transition duration parameter instead of interpolating in firmware

### Low Priority

- **Zigbee2MQTT external converter** — write a custom converter so Z2M properly maps all endpoints
- **deCONZ DDF file** — write a device description file for deCONZ/Phoscon
- **Scene support** — implement ZCL scene storage so Hue scenes work properly
- **Power-on behavior** — configure what happens when the device powers on (restore last state, default white, etc.)
- **WLED presets** — support triggering WLED presets from Zigbee scenes
- **WLED websocket API** — use WLED's websocket interface instead of HTTP POST for lower latency and bidirectional state sync

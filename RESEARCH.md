# Research Notes

Technical background, discoveries, and design decisions for the Zigbee DMX Bridge project. This document is intended for developers continuing work on the firmware. See `README.md` for usage instructions.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Hue Bridge Pairing Requirements](#hue-bridge-pairing-requirements)
- [Raw ZCL Command Handling](#raw-zcl-command-handling)
- [CIE XY Color Conversion](#cie-xy-color-conversion)
- [WiFi / 802.15.4 Coexistence](#wifi--802154-coexistence)
- [Multi-Endpoint Discovery Problem](#multi-endpoint-discovery-problem)
- [ESP-Zigbee SDK Internals](#esp-zigbee-sdk-internals)
- [ArtNet Protocol](#artnet-protocol)
- [DMX512 Protocol](#dmx512-protocol)
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
  DMX Fixtures ──(RS-485)──┤  UART1 @ 250kbaud     │
       or                  │       or               │
  ArtNet Node ──(UDP)──────┤  WiFiUDP broadcast     │
                           └──────────────────────┘
```

**Threading model:**
- The Zigbee stack runs in its own FreeRTOS task (`zigbeeTask`, 16KB stack, priority 5). It calls `esp_zb_stack_main_loop()` which never returns.
- The Arduino `loop()` runs on the default task and handles the web server and DMX output at ~40Hz.
- Light state is shared between the Zigbee task and the main loop via `lightStates[]` protected by `zbStateMutex`.

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
char manuf[] = "\x0A" "ZigbeeDMX";   // length 10 = 0x0A
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

Brightness from Zigbee (level control cluster, 0-254) is applied as a linear scale in the DMX output stage, not in the color conversion:

```cpp
float briScale = state.powerOn ? (brightness / 254.0f) : 0.0f;
dmx_r = (uint8_t)(state.red * briScale);
```

This means at brightness 127, a pure red (255, 0, 0) becomes DMX value (127, 0, 0).

### Hue/Saturation to RGB

Some Hue app operations send Hue/Saturation instead of XY. The firmware converts HSV to RGB:
- ZCL Hue: 0-254 maps to 0-360 degrees
- ZCL Saturation: 0-254 maps to 0.0-1.0
- Value is always 1.0 (brightness handled separately)

Standard HSV-to-RGB conversion is used with six 60-degree segments.

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

WiFi traffic can delay Zigbee responses. The ArtNet output was initially set to 40Hz but caused `ENOMEM` errors flooding the WiFi stack under 802.15.4 coexistence. Reduced to **2Hz (500ms interval)** which is reliable. Broadcast UDP packets were also unreliable under coexistence — solved by adding configurable unicast target IP.

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

## ArtNet Protocol

### Packet Format (OpDmx = 0x5000)

```
Offset  Size  Field              Notes
------  ----  -----------------  --------------------------------
0       8     Header             "Art-Net\0" (literal, NUL-terminated)
8       2     Opcode             0x5000 (little-endian)
10      2     Protocol Version   0x000e = 14 (big-endian)
12      1     Sequence           1-255 incrementing, 0 = disabled
13      1     Physical           Physical port (always 0 for us)
14      2     Universe           0-32767 (little-endian, 15-bit)
16      2     Data Length        2-512, must be even (big-endian)
18      N     DMX Data           Channel 1 through Channel N
```

### Key Details

- **Port:** UDP 6454 (both source and destination)
- **Broadcast:** We send to 255.255.255.255 for simplicity. Art-Net spec allows directed broadcast (e.g., x.x.x.255) or unicast.
- **Sequence:** We use incrementing 1-255 to allow receivers to detect packet reordering. 0 means "ignore sequence" per the spec.
- **Data Length:** Always 512 in our implementation (full universe). Must be even per spec.
- **Universe encoding:** The 15-bit universe field combines Net (bits 14-8), Sub-Net (bits 7-4), and Universe (bits 3-0). For simple use, the entire 15-bit value is the universe number.
- **DMX Data:** Channels 1-512 in the packet correspond to `dmxData[1]` through `dmxData[512]` in our buffer. `dmxData[0]` (the DMX start code) is not transmitted in ArtNet.
- **Frame rate:** We send at the main loop rate (~40Hz). Art-Net receivers typically expect 1-44Hz.

---

## DMX512 Protocol

### Signal Format

- **BREAK:** >= 88us of continuous LOW (mark space). We generate this by sending a 0x00 byte at 90909 baud, which produces a ~110us low signal.
- **MAB (Mark After Break):** >= 8us HIGH. Generated automatically by the UART idle line between the BREAK byte and the start of data.
- **Data:** 250000 baud, 8 data bits, no parity, 2 stop bits (8N2). Start code (0x00) followed by up to 512 channel bytes.

### UART Configuration

```cpp
uart_config.baud_rate = 250000;
uart_config.data_bits = UART_DATA_8_BITS;
uart_config.parity    = UART_PARITY_DISABLE;
uart_config.stop_bits = UART_STOP_BITS_2;
```

We use UART1 (`UART_NUM_1`). UART0 is reserved for the USB serial console on ESP32-C6.

### Break Generation

Rather than using GPIO bit-banging or the UART break feature (which doesn't work reliably on all ESP32 variants), we temporarily switch to a lower baud rate:

```cpp
uart_set_baudrate(DMX_UART, 90909);  // ~110us per byte = valid BREAK
uart_write_bytes(DMX_UART, &breakByte, 1);
uart_wait_tx_done(DMX_UART, pdMS_TO_TICKS(100));
uart_set_baudrate(DMX_UART, 250000);  // restore
```

### RS-485 Enable Pin

The transceiver enable pin (e.g., MAX485 DE/RE) is held HIGH for the entire transmission window. We don't toggle it per-byte because the DMX output is continuous.

### Channel Mapping

Each light has a configurable DMX start address (1-512) and per-channel offsets:

```
DMX address = start_addr + channel_map.red    -> Red channel
DMX address = start_addr + channel_map.green  -> Green channel
DMX address = start_addr + channel_map.blue   -> Blue channel
DMX address = start_addr + channel_map.white  -> White channel (RGBW only)
```

Default mapping: R=+0, G=+1, B=+2, W=+3. This matches common RGB/RGBW fixtures but can be rearranged for fixtures with non-standard channel orders (e.g., GRBW or BRGW).

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

### esp_dmx Library

The `esp_dmx` library v4.1.0 is incompatible with ESP-IDF v5.5.x due to the removal of `uart_periph_signal[].module`. A fix exists in PR #223 (fork: `https://github.com/joseluu/esp_dmx` branch `fix/idf-v5-uart-periph-module`). However, we opted for raw UART-based DMX output instead, which is simpler and avoids the dependency entirely.

### ArduinoJson v7

ArduinoJson v7.4.3 changed the `is<JsonArray>()` API. `doc["lights"].is<JsonArray>()` returns false even when the key contains an array. The fix is to use `doc["lights"].as<JsonArrayConst>()` and check `.isNull()` instead.

---

## Runtime Bugs Found and Fixed

### UART RX Buffer Size

ESP-IDF v5 requires the UART RX buffer to be >= `SOC_UART_FIFO_LEN` (128 bytes on ESP32-C6). Passing 0 for RX-only usage causes `uart_driver_install` to fail. Fixed by setting RX buffer to 256.

### NVS First-Boot Failure

`Preferences.begin("namespace", true)` (read-only) fails with `NOT_FOUND` if the namespace has never been written. Changed to `Preferences.begin("namespace", false)` (read-write) which creates the namespace if it doesn't exist.

### ARDUINO_USB_CDC_ON_BOOT Flag

The `ARDUINO_USB_CDC_ON_BOOT=1` build flag is incorrect for ESP32-C6. The C6 uses `HWCDC` (hardware UART over USB), not the USB CDC class used by the S3. This flag was removed from `platformio.ini`.

### sdkconfig.h Include Order

The preprocessor guard `#if defined(CONFIG_IDF_TARGET_ESP32C6)` must appear **after** `#include "sdkconfig.h"`. Without the include, the macro is undefined and the entire Zigbee implementation is silently excluded from compilation.

---

## Reference Implementation

This project is adapted from the WLED Zigbee RGB Light usermod:
- **Repository:** https://github.com/netmindz/WLED-MM/tree/zigbee-rgb-light-usermod/usermods/zigbee_rgb_light/
- **Key files:** `usermod_zigbee_rgb_light.h` (single file, ~800 lines)
- **Differences:** The usermod runs inside WLED and controls LED strips. This project is standalone and outputs DMX/ArtNet.
- **All Hue-specific workarounds** in this project originated from that usermod and were proven through months of testing with a Hue Bridge V2.

### Tested Hue Bridge Configuration

- **Device EUI64:** `40:4C:CA:FF:FE:57:2A:08`
- **Bridge IP:** `192.168.178.216`
- **Zigbee Channel:** 25
- **Device WiFi IP:** `192.168.178.110` on "MilliWatt" network
- **Light ID on bridge:** #25 (after re-pairing)
- **Metadata:** manufacturer "ZigbeeDMX", model "Test Light 1", type "Color light", reachable=true

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

- **Test integration test end-to-end** — flash ArtNet firmware, run `tools/integration_test.py`, verify all tests pass
- **Investigate Hue Bridge multi-endpoint** further — try repeated search cycles, or test with deCONZ/Phoscon as an alternative coordinator
- **OTA firmware updates** — the partition table has two OTA slots, but no update mechanism is implemented yet

### Medium Priority

- **Color temperature support** — the Hue app sends color temperature (mirek) for warm/cool white. Currently only CIE XY and HS are handled. CT could be mapped to RGBW white channel.
- **Transition time handling** — ZCL move_to_color and move_to_level include transition time fields. Currently ignored (instant transitions).
- **Web UI improvements** — output mode change should trigger immediate reconfigure without requiring manual restart
- **mDNS/service discovery** — advertise the web UI via mDNS so it's discoverable without knowing the IP
- **Zigbee state reporting** — implement `zigbeeReportState()` to send attribute reports back to the coordinator when state changes from the web UI

### Low Priority

- **Zigbee2MQTT external converter** — write a custom converter so Z2M properly maps all endpoints
- **deCONZ DDF file** — write a device description file for deCONZ/Phoscon
- **Scene support** — implement ZCL scene storage so Hue scenes work properly
- **Power-on behavior** — configure what happens when the device powers on (restore last state, default white, etc.)
- **Rate limiting** — ArtNet output at 40Hz is typical but some receivers expect lower rates. Make configurable.
- **Directed ArtNet** — send ArtNet to a specific IP instead of broadcast (reduces network load)
- **ESP-DMX library integration** — use the esp_dmx library (with PR #223 fix) for more robust DMX timing instead of raw UART baud-rate switching

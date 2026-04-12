# AGENTS.md - Coding Agent Guidelines

## Project Overview

ESP32-C6 firmware (Arduino framework on ESP-IDF v5.5) that bridges Philips Hue
(Zigbee) to WLED devices on the local network. Each configured WLED device
appears as a Zigbee Extended Color Light controllable via the WLED JSON API over
HTTP. Built with PlatformIO.

## Build Commands

```bash
# Build (default 4MB flash target)
pio run

# Build 8MB flash variant
pio run -e esp32c6-8mb

# Build and flash via USB serial
pio run -t upload

# Build and flash via OTA
pio run -t upload --upload-port zigbeewled.local

# Serial monitor
pio device monitor -b 115200

# Clean build
pio run -t clean
```

## Testing

There are no C++ unit tests. The project has a hardware-dependent integration
test and a debug tool, both in Python (`pip install requests` required).

```bash
# Full integration test (requires physical hardware: Hue Bridge + ESP32-C6 + WLED)
python3 tools/integration_test.py --device-ip <ESP32_IP>

# Run a single test category
python3 tools/integration_test.py --device-ip <IP> --test onoff
python3 tools/integration_test.py --device-ip <IP> --test color
python3 tools/integration_test.py --device-ip <IP> --test brightness
python3 tools/integration_test.py --device-ip <IP> --test accuracy
python3 tools/integration_test.py --device-ip <IP> --test colortemp
python3 tools/integration_test.py --device-ip <IP> --test rgbw

# Run only SSE tests (no Hue Bridge required)
python3 tools/integration_test.py --device-ip <IP> --test sse

# Hue Bridge debug/query tool
python3 tools/hue_debug.py
python3 tools/hue_debug.py --device-ip <IP>
python3 tools/hue_debug.py --search   # Trigger Zigbee light search
```

## Workflow Rules

- **Always flash and test on hardware before committing.** Never commit
  untested firmware. Build, flash, verify behavior on the physical device,
  then commit once it works.

## CI

GitHub Actions (`.github/workflows/build.yml`) runs `pio run -e esp32c6` on
push/PR to `main` and on version tags. No linting or test steps in CI. Tagged
pushes create GitHub Releases with firmware binaries and deploy a web installer
to GitHub Pages.

## Project Structure

```
src/                    # C++ source files (Arduino framework)
  main.cpp              # Entry point: setup() / loop()
  config_store.cpp      # NVS persistence, JSON serialization
  zigbee_manager.cpp    # Zigbee endpoints, Hue pairing, ZCL handler
  wled_output.cpp       # HTTP POST to WLED /json/state
  wled_discovery.cpp    # mDNS scan + /json/info query
  web_ui.cpp            # esp_http_server, REST API, SSE, WiFi manager, OTA
include/                # Header files (one per module)
partitions/             # Custom ESP32 flash partition tables
tools/                  # Python debug/test utilities
docs/install/           # ESP Web Tools browser-based installer
```

Each module has a matching `include/<name>.h` / `src/<name>.cpp` pair.

## Code Style (C++)

### Formatting
- **2-space indentation**, no tabs
- **K&R brace style**: opening brace on same line as statement
- No enforced line length limit (~160 chars max observed)
- Align struct member types and names with extra spaces

### Naming Conventions
| Element              | Convention          | Example                          |
|----------------------|---------------------|----------------------------------|
| Functions            | `camelCase`         | `zigbeeSetup()`, `webLoop()`     |
| Classes / Structs    | `PascalCase`        | `ConfigStore`, `LightState`      |
| Variables            | `camelCase`         | `lightCount`, `zbInitDone`       |
| Constants / Macros   | `UPPER_SNAKE_CASE`  | `MAX_LIGHTS`, `HTTP_TIMEOUT_MS`  |
| Enum values          | `UPPER_SNAKE_CASE`  | `LIGHT_TYPE_RGB`, `LIGHT_TYPE_RGBW` |
| Files                | `snake_case`        | `config_store.cpp`               |
| Global singletons    | `camelCase`         | `configStore`, `wledOutput`      |

### Includes and Headers
- Use `#pragma once` for header guards (never `#ifndef`)
- Include order in `.cpp` files: own header first, then project headers,
  then system/library headers (blank line between groups)
- System headers use `<>`, project headers use `""`

### Types
- Use sized integer types (`uint8_t`, `uint16_t`, etc.) for hardware/protocol data
- Use `static_cast<>` for numeric conversions; C-style casts only for C SDK interop
- Always use `f` suffix on float literals (`0.5f`, `255.0f`)
- Apply `const` consistently: `const` references, `const` member functions
- Use `auto` sparingly -- mainly for lambdas and complex SDK types

### Error Handling
- Use ESP-IDF logging: `ESP_LOGI`, `ESP_LOGW`, `ESP_LOGE`, `ESP_LOGD`
- Tags are short uppercase strings: `"Main"`, `"ZB"`, `"WLED"`, `"Web"`, `"Config"`
- Early return pattern for guard clauses (single-line `if (!x) return;` allowed)
- Multi-line bodies always require braces
- Check HTTP response codes; log warnings on non-200 responses

### Comments
- Every file starts with a `/* ... */` block describing the module
- Section dividers: `// ---- Description ----`
- Public API in headers: `/** ... */` Javadoc-style with `@param` / `@return`
- Inline explanations: `//` comments

### Singletons and Globals
- Declare `extern` in header, define in `.cpp`
- Classes for stateful modules (`ConfigStore`, `WledOutput`)
- Free functions with `static` module state for singleton modules

### Macros
- Wrap `#define` in `#ifndef` guards to allow override from `platformio.ini`
- Use 2-space indentation inside `#ifndef` blocks

## Code Style (Python, tools/)

- PEP 8 compliant, 4-space indentation
- `snake_case` functions/variables, `PascalCase` classes, `UPPER_SNAKE_CASE` constants
- Type hints on function signatures
- Section dividers: `# ----------- ... -----------`
- Shebang: `#!/usr/bin/env python3`
- Handle missing `requests` import with a friendly error message and `sys.exit(1)`

## Architecture Notes

- Zigbee ZBOSS stack runs in its own FreeRTOS task (16KB stack, priority 5)
- Arduino `loop()` handles WLED output at ~2Hz, WiFi reconnect, and SSE event pushing
- ESP-IDF `esp_http_server` runs in its own FreeRTOS task (no handleClient() in loop)
- ESP32-C6 shares a single 2.4GHz radio for WiFi and Zigbee (expect 1-2s latency)
- HTTP timeouts set to 20s; change detection avoids redundant WLED requests
- Changing the number of lights requires reboot, Zigbee storage erase, and re-pairing
- Flash usage is ~92% on 4MB -- near capacity; be mindful of code size
- Target board: `esp32-c6-devkitc-1` with custom partition tables including
  `zb_storage` and `zb_fct` for the Zigbee stack
- Conditional compilation with `CONFIG_IDF_TARGET_ESP32C6` guards; stub
  implementations exist for non-C6 targets

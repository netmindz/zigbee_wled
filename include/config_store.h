/*
 * Zigbee WLED Bridge - Configuration Storage
 *
 * Stores light definitions and WiFi credentials in NVS (Preferences).
 * Each light has: name, type (RGB/RGBW), and a WLED device host/port.
 */

#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include <ArduinoJson.h>

#ifndef MAX_LIGHTS
  #define MAX_LIGHTS 16
#endif

enum LightType : uint8_t {
  LIGHT_TYPE_RGB  = 3,
  LIGHT_TYPE_RGBW = 4,
};

struct LightConfig {
  bool     active;
  char     name[32];
  LightType type;
  char     wledHost[64];    // IP address or hostname of WLED device
  uint16_t wledPort;        // HTTP port (default 80)
};

// Current state of a light (set by Zigbee, read by WLED output)
struct LightState {
  bool     powerOn;
  uint8_t  brightness;   // 0-254 (Zigbee scale)
  uint8_t  red;
  uint8_t  green;
  uint8_t  blue;
  uint8_t  white;        // only used for RGBW
  float    colorX;       // CIE 1931 x (0.0-1.0), last received from ZCL
  float    colorY;       // CIE 1931 y (0.0-1.0), last received from ZCL

  // Transition support: when a transition is active, we interpolate from
  // start values to current (target) values over the transition period.
  // All times in milliseconds (from millis()).
  bool     transitioning;
  uint32_t transitionStart;   // millis() when transition began
  uint32_t transitionEnd;     // millis() when transition should finish
  uint8_t  startBrightness;
  uint8_t  startRed;
  uint8_t  startGreen;
  uint8_t  startBlue;
  uint8_t  startWhite;
};

class ConfigStore {
public:
  void begin();

  // Light configuration
  uint8_t getLightCount() const { return lightCount; }
  const LightConfig& getLight(uint8_t index) const { return lights[index]; }
  LightConfig& getLightMut(uint8_t index) { return lights[index]; }

  // Add a light with defaults, returns index or -1 if full
  int addLight(const char* name, LightType type, const char* wledHost, uint16_t wledPort = 80);

  // Remove a light by index, shifts remaining lights down
  bool removeLight(uint8_t index);

  // Update a light's config
  bool updateLight(uint8_t index, const LightConfig& cfg);

  // Persist all config to NVS
  void save();

  // Load config from NVS
  void load();

  // Export config as JSON (for web UI)
  void toJson(JsonDocument& doc) const;

  // Import config from JSON (from web UI)
  bool fromJson(const JsonDocument& doc);

  // Reset to factory defaults (clears all lights and WiFi config)
  void factoryReset();

private:
  Preferences prefs;
  uint8_t lightCount = 0;
  LightConfig lights[MAX_LIGHTS] = {};
};

extern ConfigStore configStore;

/*
 * Zigbee DMX Bridge - Configuration Storage
 *
 * Stores light definitions, output mode, and WiFi credentials in NVS (Preferences).
 * Each light has: name, type (RGB/RGBW), DMX start address,
 * and channel-to-offset mapping.
 */

#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include <ArduinoJson.h>

#ifndef MAX_LIGHTS
  #define MAX_LIGHTS 16
#endif

// Channel mapping: which DMX offset corresponds to which color channel
struct DmxChannelMap {
  uint8_t red;    // offset from start address for red (0-based)
  uint8_t green;  // offset for green
  uint8_t blue;   // offset for blue
  uint8_t white;  // offset for white (only used if type == RGBW)
};

enum LightType : uint8_t {
  LIGHT_TYPE_RGB  = 3,
  LIGHT_TYPE_RGBW = 4,
};

enum OutputMode : uint8_t {
  OUTPUT_MODE_WIRED_DMX = 0,
  OUTPUT_MODE_ARTNET    = 1,
};

struct OutputConfig {
  OutputMode mode = OUTPUT_MODE_WIRED_DMX;
  // Wired DMX settings
  int8_t txPin  = 2;   // GPIO for DMX TX (default GPIO2)
  int8_t enPin  = 4;   // GPIO for RS-485 enable (default GPIO4, -1 = not used)
  // ArtNet settings
  uint16_t artnetUniverse = 0;  // ArtNet universe (0-32767)
};

struct LightConfig {
  bool     active;
  char     name[32];
  LightType type;
  uint16_t dmxStartAddr;  // 1-512
  DmxChannelMap channelMap;
};

// Current state of a light (set by Zigbee, read by DMX output)
struct LightState {
  bool     powerOn;
  uint8_t  brightness;   // 0-254 (Zigbee scale)
  uint8_t  red;
  uint8_t  green;
  uint8_t  blue;
  uint8_t  white;        // only used for RGBW

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

  // Output configuration
  const OutputConfig& getOutputConfig() const { return outputCfg; }
  void setOutputConfig(const OutputConfig& cfg) { outputCfg = cfg; }

  // Add a light with defaults, returns index or -1 if full
  int addLight(const char* name, LightType type, uint16_t dmxAddr);

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
  OutputConfig outputCfg;
};

extern ConfigStore configStore;

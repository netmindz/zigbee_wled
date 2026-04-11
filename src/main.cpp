/*
 * Zigbee DMX Bridge - Main Entry Point
 *
 * ESP32-C6 firmware that:
 * 1. Provides a captive portal for WiFi setup
 * 2. Hosts a web UI for configuring RGB/RGBW DMX lights
 * 3. Presents each light as a Zigbee HA Color Dimmable Light (Hue-compatible)
 * 4. Outputs light states via wired DMX512 or ArtNet (UDP), configurable via web UI
 *
 * Architecture:
 *   Hue Bridge  --(Zigbee ZCL)--> ESP32-C6 --(DMX512 / ArtNet)--> RGB/RGBW fixtures
 *       |                             |
 *   Controls color/brightness    Web config UI
 *   via CIE XY color space       for light setup
 */

#include <Arduino.h>
#include <ArduinoOTA.h>
#include "config_store.h"
#include "dmx_output.h"
#include "web_ui.h"
#include "zigbee_manager.h"

// DMX update rate (Hz)
static const unsigned long DMX_UPDATE_INTERVAL_MS = 25;  // ~40 Hz
static unsigned long lastDmxUpdate = 0;

void setup() {
  Serial.begin(115200);
  delay(1000);

  ESP_LOGI("Main", "=== Zigbee DMX Bridge ===");
  ESP_LOGI("Main", "Firmware v0.1.0");

  // 1. Load configuration from NVS
  configStore.begin();
  ESP_LOGI("Main", "Loaded %d light(s) from config", configStore.getLightCount());
  ESP_LOGI("Main", "Output mode: %s",
           configStore.getOutputConfig().mode == OUTPUT_MODE_ARTNET ? "ArtNet" : "Wired DMX");

  // 2. Init Zigbee platform (must be before WiFi starts)
  zigbeeSetup();

  // 3. Start WiFi + Web UI (this also triggers zigbeeStart() on WiFi connect)
  //    DMX output is initialized after WiFi for ArtNet mode.
  webSetup();

  // 3b. Setup ArduinoOTA for network-based firmware upload from PlatformIO
  ArduinoOTA.setHostname("zigbeedmx");
  ArduinoOTA.onStart([]() {
    ESP_LOGI("OTA", "OTA update starting...");
    dmxOutput.stop();  // Stop DMX output during OTA
  });
  ArduinoOTA.onEnd([]() {
    ESP_LOGI("OTA", "OTA update complete, rebooting...");
  });
  ArduinoOTA.onError([](ota_error_t error) {
    ESP_LOGE("OTA", "OTA error: %u", error);
  });
  ArduinoOTA.begin();

  // 4. Init DMX output
  //    For wired DMX this starts immediately.
  //    For ArtNet, it will lazy-init the UDP socket when WiFi is available.
  dmxOutput.begin();
}

// Interpolate a single channel value for smooth transitions
static uint8_t interpolate(uint8_t start, uint8_t end, float progress) {
  return static_cast<uint8_t>(start + (static_cast<float>(end) - static_cast<float>(start)) * progress);
}

void loop() {
  // Process web server tasks (DNS for captive portal, WiFi reconnect check)
  webLoop();

  // Handle OTA updates
  ArduinoOTA.handle();

  // Update DMX output at fixed rate
  unsigned long now = millis();
  if (now - lastDmxUpdate >= DMX_UPDATE_INTERVAL_MS) {
    lastDmxUpdate = now;

    uint8_t count = configStore.getLightCount();
    if (count > 0) {
      // Build array of current states from Zigbee
      LightState states[MAX_LIGHTS];
      for (uint8_t i = 0; i < count; i++) {
        states[i] = zigbeeGetLightState(i);

        // Apply transition interpolation if active
        if (states[i].transitioning) {
          if (now >= states[i].transitionEnd) {
            // Transition complete — target values are already in place
            states[i].transitioning = false;
          } else {
            uint32_t elapsed = now - states[i].transitionStart;
            uint32_t duration = states[i].transitionEnd - states[i].transitionStart;
            float progress = (duration > 0) ? static_cast<float>(elapsed) / static_cast<float>(duration) : 1.0f;
            if (progress > 1.0f) progress = 1.0f;

            // Interpolate from start values toward target values
            states[i].brightness = interpolate(states[i].startBrightness, states[i].brightness, progress);
            states[i].red   = interpolate(states[i].startRed, states[i].red, progress);
            states[i].green = interpolate(states[i].startGreen, states[i].green, progress);
            states[i].blue  = interpolate(states[i].startBlue, states[i].blue, progress);
            states[i].white = interpolate(states[i].startWhite, states[i].white, progress);
          }
        }
      }

      // Update DMX output
      dmxOutput.update(&configStore.getLight(0), states, count);
    }
  }
}

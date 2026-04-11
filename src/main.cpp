/*
 * Zigbee DMX Bridge - Main Entry Point
 *
 * ESP32-C6 firmware that:
 * 1. Provides a captive portal for WiFi setup
 * 2. Hosts a web UI for configuring RGB/RGBW DMX lights
 * 3. Presents each light as a Zigbee HA Color Dimmable Light (Hue-compatible)
 * 4. Outputs light states via DMX512 through a UART/RS-485 transceiver
 *
 * Architecture:
 *   Hue Bridge  --(Zigbee ZCL)--> ESP32-C6 --(DMX512)--> RGB/RGBW fixtures
 *       |                             |
 *   Controls color/brightness    Web config UI
 *   via CIE XY color space       for light setup
 */

#include <Arduino.h>
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

  // 2. Init Zigbee platform (must be before WiFi starts)
  zigbeeSetup();

  // 3. Init DMX output
  dmxOutput.begin();

  // 4. Start WiFi + Web UI (this also triggers zigbeeStart() on WiFi connect)
  webSetup();
}

void loop() {
  // Process web server tasks (DNS for captive portal, WiFi reconnect check)
  webLoop();

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
      }

      // Update DMX output
      dmxOutput.update(&configStore.getLight(0), states, count);
    }
  }
}

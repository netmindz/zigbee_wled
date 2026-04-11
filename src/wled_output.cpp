/*
 * Zigbee WLED Bridge - WLED Output Implementation
 *
 * Sends HTTP POST requests to WLED devices' /json/state endpoint
 * to control color, brightness, and power state.
 *
 * Uses the Arduino HTTPClient library for HTTP communication.
 * Connection timeout set to 20s due to WiFi/Zigbee coexistence latency.
 */

#include "wled_output.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

WledOutput wledOutput;

// HTTP timeout in ms — needs to be generous due to WiFi/Zigbee coexistence
static const int HTTP_TIMEOUT_MS = 20000;

// Consecutive error tracking per device
static uint32_t sendErrors[MAX_LIGHTS] = {};

void WledOutput::begin() {
  if (initialized) return;

  memset(lastSent, 0, sizeof(lastSent));
  memset(sendErrors, 0, sizeof(sendErrors));

  initialized = true;
  ESP_LOGI("WLED", "WLED output initialized");
}

bool WledOutput::stateChanged(uint8_t index, const LightState& state) const {
  if (index >= MAX_LIGHTS) return false;
  const SentState& last = lastSent[index];

  // Always send if we haven't sent yet
  if (!last.valid) return true;

  // Check for any change
  return (last.powerOn != state.powerOn ||
          last.brightness != state.brightness ||
          last.red != state.red ||
          last.green != state.green ||
          last.blue != state.blue ||
          last.white != state.white);
}

bool WledOutput::sendToWled(const LightConfig& cfg, const LightState& state) {
  if (!WiFi.isConnected()) return false;
  if (cfg.wledHost[0] == '\0') return false;

  // Build the URL
  String url = "http://";
  url += cfg.wledHost;
  if (cfg.wledPort != 80) {
    url += ":";
    url += cfg.wledPort;
  }
  url += "/json/state";

  // Build JSON payload
  JsonDocument doc;

  if (!state.powerOn) {
    // Just turn off
    doc["on"] = false;
  } else {
    doc["on"] = true;

    // Map Zigbee brightness (0-254) to WLED brightness (0-255)
    uint8_t wledBri = (state.brightness >= 254) ? 255
                    : static_cast<uint8_t>(state.brightness + (state.brightness > 0 ? 1 : 0));
    doc["bri"] = wledBri;

    // Build segment with solid effect and color
    JsonArray seg = doc["seg"].to<JsonArray>();
    JsonObject seg0 = seg.add<JsonObject>();
    seg0["fx"] = 0;  // Solid effect

    JsonArray col = seg0["col"].to<JsonArray>();
    JsonArray color0 = col.add<JsonArray>();
    color0.add(state.red);
    color0.add(state.green);
    color0.add(state.blue);

    // Add white channel for RGBW fixtures
    if (cfg.type == LIGHT_TYPE_RGBW) {
      color0.add(state.white);
    }
  }

  // Serialize
  String payload;
  serializeJson(doc, payload);

  // Send HTTP POST
  HTTPClient http;
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);

  if (!http.begin(url)) {
    ESP_LOGW("WLED", "HTTP begin failed for %s", cfg.wledHost);
    return false;
  }

  http.addHeader("Content-Type", "application/json");
  int httpCode = http.POST(payload);
  http.end();

  if (httpCode == 200) {
    return true;
  } else {
    ESP_LOGW("WLED", "HTTP POST to %s returned %d", cfg.wledHost, httpCode);
    return false;
  }
}

void WledOutput::update(const LightConfig* lights, const LightState* states, uint8_t count) {
  if (!initialized) return;
  if (!WiFi.isConnected()) return;

  for (uint8_t i = 0; i < count; i++) {
    if (!lights[i].active) continue;
    if (lights[i].wledHost[0] == '\0') continue;

    // Only send if state changed (avoid redundant HTTP calls)
    if (!stateChanged(i, states[i])) continue;

    bool ok = sendToWled(lights[i], states[i]);

    if (ok) {
      // Record sent state
      lastSent[i].valid = true;
      lastSent[i].powerOn = states[i].powerOn;
      lastSent[i].brightness = states[i].brightness;
      lastSent[i].red = states[i].red;
      lastSent[i].green = states[i].green;
      lastSent[i].blue = states[i].blue;
      lastSent[i].white = states[i].white;

      if (sendErrors[i] > 0) {
        ESP_LOGI("WLED", "Device %s recovered after %lu errors",
                 lights[i].wledHost, sendErrors[i]);
      }
      sendErrors[i] = 0;
    } else {
      sendErrors[i]++;
      // Invalidate last-sent so we retry on next cycle
      lastSent[i].valid = false;
      // Rate-limit error logging: only log at powers of 2
      if ((sendErrors[i] & (sendErrors[i] - 1)) == 0) {
        ESP_LOGW("WLED", "Device %s: %lu consecutive send failures",
                 lights[i].wledHost, sendErrors[i]);
      }
    }
  }
}

void WledOutput::stop() {
  if (!initialized) return;

  memset(lastSent, 0, sizeof(lastSent));
  memset(sendErrors, 0, sizeof(sendErrors));

  initialized = false;
  ESP_LOGI("WLED", "WLED output stopped");
}

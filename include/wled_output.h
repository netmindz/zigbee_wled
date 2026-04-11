/*
 * Zigbee WLED Bridge - WLED Output
 *
 * Controls WLED devices via their JSON API over HTTP.
 * Each configured light maps to a WLED device on the network.
 * Sends HTTP POST to /json/state to set color, brightness, and on/off.
 *
 * Rate limited to ~2Hz per device to avoid flooding under
 * WiFi/Zigbee coexistence constraints.
 */

#pragma once

#include <Arduino.h>
#include "config_store.h"

class WledOutput {
public:
  /**
   * Initialize the output subsystem.
   * Call after configStore.begin() and WiFi is available.
   */
  void begin();

  /**
   * Update all WLED devices from light states.
   * Called at a fixed rate from the main loop (~2Hz).
   * Only sends updates for lights whose state has changed.
   */
  void update(const LightConfig* lights, const LightState* states, uint8_t count);

  /**
   * Stop all output and release resources.
   */
  void stop();

  /**
   * Return true if the output subsystem is initialized and active.
   */
  bool isRunning() const { return initialized; }

private:
  bool initialized = false;

  // Track last-sent state per light to avoid redundant HTTP calls
  struct SentState {
    bool     valid;       // true if we've sent at least once
    bool     powerOn;
    uint8_t  brightness;
    uint8_t  red;
    uint8_t  green;
    uint8_t  blue;
    uint8_t  white;
  };
  SentState lastSent[MAX_LIGHTS] = {};

  // Send state to a single WLED device via HTTP POST
  bool sendToWled(const LightConfig& cfg, const LightState& state);

  // Check if state has changed since last send
  bool stateChanged(uint8_t index, const LightState& state) const;
};

extern WledOutput wledOutput;

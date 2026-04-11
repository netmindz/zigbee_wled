/*
 * Zigbee DMX Bridge - DMX Output
 *
 * Dual-mode DMX output supporting:
 * 1. Wired DMX512 via esp_dmx library through an RS-485 transceiver
 * 2. ArtNet (Art-Net DMX over UDP broadcast)
 *
 * The output mode is selected at runtime via the OutputConfig from config_store.
 */

#pragma once

#include <Arduino.h>
#include "config_store.h"

class DmxOutput {
public:
  /**
   * Initialize the output subsystem based on the current OutputConfig.
   * Call after configStore.begin().
   */
  void begin();

  /**
   * Re-initialize with a new output config (e.g. after web UI change).
   * Stops current output, reconfigures, and restarts.
   */
  void reconfigure();

  /**
   * Update DMX channel data from light states and send a frame.
   * Called at a fixed rate from the main loop (~40Hz).
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

  /**
   * Return the current output mode.
   */
  OutputMode getMode() const { return currentMode; }

private:
  bool initialized = false;
  OutputMode currentMode = OUTPUT_MODE_WIRED_DMX;
  uint8_t dmxData[513] = {};  // slot 0 = start code (0x00), 1-512 = channels

  // ArtNet state
  uint8_t artnetSequence = 0;
  uint32_t artnetSendErrors = 0;    // Consecutive send failures

  // Wired DMX state
  int dmxPort = -1;  // esp_dmx port number, or -1 if not using

  // Populate dmxData[] from light configs and states
  void buildDmxFrame(const LightConfig* lights, const LightState* states, uint8_t count);

  // Output backends
  void beginWiredDmx();
  void beginArtNet();
  void sendWiredDmx();
  void sendArtNet();
  void stopWiredDmx();
  void stopArtNet();
};

extern DmxOutput dmxOutput;

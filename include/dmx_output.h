/*
 * Zigbee DMX Bridge - DMX Output
 *
 * Manages DMX512 output via UART through an RS-485 transceiver.
 * Uses ESP32 UART hardware directly (no external library dependency).
 *
 * DMX512 protocol:
 * - BREAK: >=88us low
 * - MAB (Mark After Break): >=8us high
 * - Start code (0x00) + up to 512 data bytes at 250kbaud, 8N2
 */

#pragma once

#include <Arduino.h>
#include "config_store.h"

class DmxOutput {
public:
  void begin();
  void update(const LightConfig* lights, const LightState* states, uint8_t count);
  void stop();

private:
  bool initialized = false;
  uint8_t dmxData[513] = {};  // slot 0 = start code (0x00), 1-512 = channels

  void sendBreak();
  void sendData();
};

extern DmxOutput dmxOutput;

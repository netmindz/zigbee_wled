/*
 * Zigbee DMX Bridge - Zigbee Manager
 *
 * Manages the Zigbee stack running as End Device(s).
 * Creates one Zigbee HA Color Dimmable Light endpoint per configured light.
 * All critical Hue pairing requirements from the WLED research are applied.
 *
 * Based on the proven implementation from:
 * https://github.com/netmindz/WLED-MM/tree/zigbee-rgb-light-usermod/usermods/zigbee_rgb_light/
 */

#pragma once

#include <Arduino.h>
#include "config_store.h"

// Initialize Zigbee platform (call from setup(), before WiFi)
void zigbeeSetup();

// Start the Zigbee task (call after WiFi STA connects)
void zigbeeStart();

// Check if Zigbee has joined a network
bool zigbeeIsPaired();

// Get Zigbee EUI64 as string
const char* zigbeeGetEUI64();

// Get the current state of a light (set by Zigbee commands)
const LightState& zigbeeGetLightState(uint8_t index);

// Report a state change to the Zigbee coordinator (e.g. from web UI)
void zigbeeReportState(uint8_t index, const LightState& state);

// Re-register endpoints after config change (requires Zigbee restart)
void zigbeeReconfigure();

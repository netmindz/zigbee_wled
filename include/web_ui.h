/*
 * Zigbee DMX Bridge - Web UI & WiFi Manager
 *
 * Handles:
 * - Initial WiFi configuration via captive portal (AP mode)
 * - Web-based configuration UI for defining lights
 * - REST API for light configuration CRUD
 * - Status page showing Zigbee pairing state
 */

#pragma once

#include <Arduino.h>

void webSetup();
void webLoop();

// Call when WiFi STA connects successfully
void webOnWifiConnected();

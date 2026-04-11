/*
 * Zigbee WLED Bridge - Web UI & WiFi Manager
 *
 * Handles:
 * - Initial WiFi configuration via captive portal (AP mode)
 * - Web-based configuration UI for WLED light definitions
 * - REST API for light configuration CRUD
 * - WLED device discovery via mDNS
 * - Status page showing Zigbee pairing state
 */

#pragma once

#include <Arduino.h>

void webSetup();
void webLoop();

// Call when WiFi STA connects successfully
void webOnWifiConnected();

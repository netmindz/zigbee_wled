/*
 * Zigbee WLED Bridge - WLED Device Discovery
 *
 * Discovers WLED devices on the local network using mDNS.
 * WLED devices advertise as _wled._tcp with a TXT record containing
 * the MAC address. After mDNS discovery, queries each device's
 * /json/info endpoint to get device name, LED count, RGBW capability, etc.
 */

#pragma once

#include <Arduino.h>
#include <vector>

struct WledDeviceInfo {
  char name[64];       // WLED device name (from /json/info)
  char host[64];       // IP address as string
  char hostname[64];   // mDNS hostname with .local suffix (e.g. "wled-abcdef.local")
  uint16_t port;       // HTTP port (usually 80)
  char mac[18];        // MAC address (from /json/info)
  uint16_t ledCount;   // Number of LEDs
  bool isRGBW;         // Has white channel
  char version[16];    // WLED firmware version
};

/**
 * Scan the local network for WLED devices via mDNS.
 * Queries each discovered device's /json/info for metadata.
 *
 * @param results  Vector to fill with discovered devices
 * @param timeoutMs  mDNS scan timeout in milliseconds (default 8000)
 * @return Number of devices found
 */
int wledDiscover(std::vector<WledDeviceInfo>& results, uint32_t timeoutMs = 8000);

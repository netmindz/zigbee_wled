/*
 * Zigbee WLED Bridge - WLED Device Discovery Implementation
 *
 * Uses ESP32 mDNS to find WLED devices on the network, then queries
 * each device's /json/info endpoint for detailed information.
 *
 * WLED devices advertise as _http._tcp. We identify them by checking
 * if the hostname starts with "wled" or by querying the /json/info endpoint.
 */

#include "wled_discovery.h"
#include <WiFi.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// HTTP timeout for querying WLED device info (generous due to coexistence)
static const int DISCOVERY_HTTP_TIMEOUT_MS = 10000;

// Query a single WLED device for its info
static bool queryWledInfo(const char* host, uint16_t port, WledDeviceInfo& info) {
  String url = "http://";
  url += host;
  if (port != 80) {
    url += ":";
    url += port;
  }
  url += "/json/info";

  HTTPClient http;
  http.setConnectTimeout(DISCOVERY_HTTP_TIMEOUT_MS);
  http.setTimeout(DISCOVERY_HTTP_TIMEOUT_MS);

  if (!http.begin(url)) {
    return false;
  }

  int httpCode = http.GET();
  if (httpCode != 200) {
    http.end();
    return false;
  }

  String body = http.getString();
  http.end();

  // Parse JSON response
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    ESP_LOGW("Discovery", "JSON parse error from %s: %s", host, err.c_str());
    return false;
  }

  // Extract device info
  const char* name = doc["name"] | "WLED";
  strlcpy(info.name, name, sizeof(info.name));

  const char* mac = doc["mac"] | "";
  strlcpy(info.mac, mac, sizeof(info.mac));

  const char* ver = doc["ver"] | "";
  strlcpy(info.version, ver, sizeof(info.version));

  // LED info is under "leds" object
  JsonObjectConst leds = doc["leds"].as<JsonObjectConst>();
  if (!leds.isNull()) {
    info.ledCount = leds["count"] | 0;
    info.isRGBW = leds["rgbw"] | false;
  } else {
    info.ledCount = 0;
    info.isRGBW = false;
  }

  return true;
}

int wledDiscover(std::vector<WledDeviceInfo>& results, uint32_t timeoutMs) {
  results.clear();

  if (!WiFi.isConnected()) {
    ESP_LOGW("Discovery", "WiFi not connected, cannot discover WLED devices");
    return 0;
  }

  // Initialize mDNS if not already running
  if (!MDNS.begin("zigbeewled")) {
    ESP_LOGW("Discovery", "mDNS init failed, retrying...");
    delay(100);
    if (!MDNS.begin("zigbeewled")) {
      ESP_LOGE("Discovery", "mDNS init failed twice, aborting discovery");
      return 0;
    }
  }

  ESP_LOGI("Discovery", "Scanning for WLED devices via mDNS...");

  // Query for _http._tcp services — WLED devices advertise as HTTP services
  int numServices = MDNS.queryService("http", "tcp");

  ESP_LOGI("Discovery", "mDNS found %d HTTP services", numServices);

  for (int i = 0; i < numServices; i++) {
    String hostname = MDNS.hostname(i);
    IPAddress ip = MDNS.address(i);
    uint16_t port = MDNS.port(i);

    // Filter for likely WLED devices by hostname
    String hostLower = hostname;
    hostLower.toLowerCase();

    // WLED hostnames typically start with "wled"
    // But we also try querying any device — /json/info will fail on non-WLED
    bool likelyWled = hostLower.startsWith("wled");

    if (!likelyWled) {
      // Skip non-WLED devices to avoid slow HTTP timeouts
      continue;
    }

    String ipStr = ip.toString();

    ESP_LOGI("Discovery", "Checking device: %s (%s:%d)",
             hostname.c_str(), ipStr.c_str(), port);

    WledDeviceInfo info = {};
    strlcpy(info.host, ipStr.c_str(), sizeof(info.host));
    info.port = port;

    if (queryWledInfo(ipStr.c_str(), port, info)) {
      ESP_LOGI("Discovery", "Found WLED device: %s (%s) - %d LEDs, RGBW=%d, v%s",
               info.name, info.host, info.ledCount, info.isRGBW, info.version);
      results.push_back(info);
    } else {
      ESP_LOGD("Discovery", "Device %s is not a WLED device or not responding",
               hostname.c_str());
    }
  }

  ESP_LOGI("Discovery", "Discovery complete: found %d WLED device(s)", results.size());
  return results.size();
}

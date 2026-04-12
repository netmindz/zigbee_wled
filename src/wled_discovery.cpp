/*
 * Zigbee WLED Bridge - WLED Device Discovery Implementation
 *
 * Uses ESP-IDF's native mDNS API (mdns_query_ptr) to find WLED devices
 * on the network via the _wled._tcp service type, then queries each
 * device's /json/info endpoint for detailed information.
 *
 * WLED devices advertise a dedicated _wled._tcp mDNS service, which
 * allows discovery regardless of the device's hostname.
 *
 * Uses multiple mDNS query passes with de-duplication because low-power
 * devices (especially ESP8266-based WLED) may not respond to every query,
 * and WiFi/Zigbee coexistence on ESP32-C6 adds latency.
 */

#include "wled_discovery.h"
#include <WiFi.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <mdns.h>

// HTTP timeout for querying WLED device info (generous due to coexistence)
static const int DISCOVERY_HTTP_TIMEOUT_MS = 10000;

// mDNS query passes — multiple passes catch devices that miss a single query
static const int NUM_PASSES = 3;
static const uint32_t PASS_TIMEOUT_MS = 4000;

// Maximum devices we can discover
static const int MAX_MDNS_DEVICES = 20;

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

// Helper: extract IPv4 address string from an mDNS result
static bool mdnsResultToIP(mdns_result_t* r, char* ipStr, size_t ipStrLen) {
  if (!r || !r->addr) return false;

  mdns_ip_addr_t* addr = r->addr;
  // Prefer IPv4
  while (addr) {
    if (addr->addr.type == ESP_IPADDR_TYPE_V4) {
      snprintf(ipStr, ipStrLen, IPSTR, IP2STR(&addr->addr.u_addr.ip4));
      return true;
    }
    addr = addr->next;
  }
  return false;
}

// ---- Intermediate storage for mDNS results across passes ----
struct MdnsDevice {
  char hostname[64];
  char hostLocal[80];
  char ip[64];
  uint16_t port;
};

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

  ESP_LOGI("Discovery", "Scanning for WLED devices via mDNS (_wled._tcp)...");

  // Run multiple mDNS query passes and merge results.  Low-power devices
  // (especially ESP8266-based WLED) may not respond to every mDNS query,
  // and WiFi/Zigbee coexistence adds latency.  We run multiple passes
  // and de-duplicate by hostname.
  MdnsDevice found[MAX_MDNS_DEVICES];
  int foundCount = 0;

  for (int pass = 0; pass < NUM_PASSES; pass++) {
    if (pass > 0) {
      delay(500);
    }

    ESP_LOGI("Discovery", "mDNS query pass %d/%d, timeout: %lu ms",
             pass + 1, NUM_PASSES, (unsigned long)PASS_TIMEOUT_MS);

    mdns_result_t* mdnsResults = nullptr;
    esp_err_t err = mdns_query_ptr("_wled", "_tcp", PASS_TIMEOUT_MS, 20, &mdnsResults);
    if (err != ESP_OK) {
      ESP_LOGW("Discovery", "mDNS query failed: %s", esp_err_to_name(err));
      continue;
    }

    if (!mdnsResults) {
      ESP_LOGI("Discovery", "mDNS pass %d found 0 WLED services", pass + 1);
      continue;
    }

    // Merge new results into found[], de-duplicating by hostname
    for (mdns_result_t* r = mdnsResults; r != nullptr; r = r->next) {
      const char* hostname = r->hostname;
      if (!hostname) continue;

      // Check for duplicate
      bool dup = false;
      for (int j = 0; j < foundCount; j++) {
        if (strcasecmp(found[j].hostname, hostname) == 0) {
          dup = true;
          break;
        }
      }
      if (dup || foundCount >= MAX_MDNS_DEVICES) continue;

      // Get IPv4 address
      char ipStr[64];
      if (!mdnsResultToIP(r, ipStr, sizeof(ipStr))) continue;

      MdnsDevice& d = found[foundCount];
      strlcpy(d.hostname, hostname, sizeof(d.hostname));
      snprintf(d.hostLocal, sizeof(d.hostLocal), "%s.local", hostname);
      strlcpy(d.ip, ipStr, sizeof(d.ip));
      d.port = r->port;
      foundCount++;

      ESP_LOGI("Discovery", "Pass %d: found WLED device: %s (%s:%d)",
               pass + 1, hostname, ipStr, d.port);
    }

    mdns_query_results_free(mdnsResults);
  }

  ESP_LOGI("Discovery", "mDNS found %d unique WLED devices across %d passes",
           foundCount, NUM_PASSES);

  if (foundCount == 0) {
    return 0;
  }

  // Query each unique device for its WLED info
  for (int i = 0; i < foundCount; i++) {
    const MdnsDevice& d = found[i];

    ESP_LOGI("Discovery", "Checking device: %s (%s:%d)", d.hostLocal, d.ip, d.port);

    WledDeviceInfo info = {};
    strlcpy(info.host, d.ip, sizeof(info.host));
    strlcpy(info.hostname, d.hostLocal, sizeof(info.hostname));
    info.port = d.port;

    // Query using IP for speed (already resolved via mDNS)
    if (queryWledInfo(d.ip, d.port, info)) {
      ESP_LOGI("Discovery", "Found WLED device: %s (%s / %s) - %d LEDs, RGBW=%d, v%s",
               info.name, info.hostname, info.host, info.ledCount, info.isRGBW, info.version);
      results.push_back(info);
    } else {
      ESP_LOGD("Discovery", "Device %s is not a WLED device or not responding",
               d.hostname);
    }
  }

  ESP_LOGI("Discovery", "Discovery complete: found %d WLED device(s)", (int)results.size());
  return results.size();
}

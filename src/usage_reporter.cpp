/*
 * Zigbee WLED Bridge - Usage Reporter Implementation
 *
 * Builds the device-info payload consumed by the web UI for usage reporting.
 * The browser POSTs directly to https://usage.wled.me/api/usage/upgrade after
 * the user consents — no outbound HTTP from the firmware is required.
 *
 * Device ID algorithm mirrors WLED's getDeviceId() in wled00/util.cpp:
 *   fingerprint = hex(MAC XOR flashSize XOR chipModel/revision)  [16 chars]
 *   firstHash   = SHA1(fingerprint)                              [40 hex chars]
 *   deviceId    = firstHash + SHA1(firstHash)[-2:]              [42 chars]
 *
 * Consent preferences are persisted in NVS namespace "usage":
 *   prevVer      — firmware version at the time consent was last recorded
 *   neverAsk     — user permanently declined (never show dialog again)
 *   alwaysReport — user permanently opted in (auto-report on version change)
 */

#include "usage_reporter.h"
#include "config_store.h"

#include <Preferences.h>
#include <ArduinoJson.h>
#include <esp_chip_info.h>
#include <esp_mac.h>
#include <mbedtls/sha1.h>

// ---- Build-time constants ----

#ifndef FIRMWARE_VERSION
  #define FIRMWARE_VERSION "0.0.0"
#endif

#ifndef FIRMWARE_RELEASE_NAME
  #define FIRMWARE_RELEASE_NAME "ZigbeeWLED"
#endif

static const char* NVS_NS       = "usage";
static const char* NVS_PREV_VER = "prevVer";
static const char* NVS_NEVER    = "neverAsk";
static const char* NVS_ALWAYS   = "alwaysReport";

// ---- SHA-1 helper ----

static String sha1HexStr(const String& s) {
  uint8_t hash[20];
  mbedtls_sha1(reinterpret_cast<const uint8_t*>(s.c_str()), s.length(), hash);
  char buf[41];
  for (int i = 0; i < 20; i++) {
    snprintf(buf + i * 2, 3, "%02x", hash[i]);
  }
  return String(buf);
}

// ---- Device ID (mirrors WLED util.cpp) ----

static String getDeviceId() {
  uint32_t fp[2] = {0, 0};
  esp_efuse_mac_get_default(reinterpret_cast<uint8_t*>(fp));

  esp_chip_info_t chip;
  esp_chip_info(&chip);
  fp[1] ^= static_cast<uint32_t>(ESP.getFlashChipSize());
  fp[0] ^= chip.revision | (static_cast<uint32_t>(chip.model) << 16);

  char fpStr[17];
  snprintf(fpStr, sizeof(fpStr), "%08X%08X", fp[1], fp[0]);

  String firstHash  = sha1HexStr(String(fpStr));
  String secondHash = sha1HexStr(firstHash);
  return firstHash + secondHash.substring(secondHash.length() - 2);
}

// ---- NVS helpers ----

static String loadPrevVer() {
  Preferences p;
  p.begin(NVS_NS, true);
  String v = p.getString(NVS_PREV_VER, "");
  p.end();
  return v;
}

static bool loadBool(const char* key) {
  Preferences p;
  p.begin(NVS_NS, true);
  bool v = p.getBool(key, false);
  p.end();
  return v;
}

// ---- Public API ----

String usageGetInfoJson() {
  String prevVer      = loadPrevVer();
  bool   neverAsk     = loadBool(NVS_NEVER);
  bool   alwaysReport = loadBool(NVS_ALWAYS);

  const char* currentVersion = FIRMWARE_VERSION;
  bool versionChanged    = (prevVer != currentVersion);
  bool shouldPrompt      = !neverAsk && !alwaysReport && versionChanged;
  bool browserAutoReport = alwaysReport && versionChanged;

  JsonDocument doc;
  doc["deviceId"]         = getDeviceId();
  doc["version"]          = currentVersion;
  doc["previousVersion"]  = prevVer.isEmpty() ? "0.0.0" : prevVer.c_str();
  doc["releaseName"]      = FIRMWARE_RELEASE_NAME;
  doc["chip"]             = "esp32c6";
  doc["ledCount"]         = static_cast<int>(configStore.getLightCount());
  doc["isMatrix"]         = false;
  doc["bootloaderSHA256"] = "unknown";
  doc["brand"]            = "Zigbee WLED Bridge";
  doc["flashSize"]        = static_cast<int>(ESP.getFlashChipSize() / (1024 * 1024));
  doc["repo"]             = "netmindz/zigbee_wled";

  JsonArray integrations = doc["integrations"].to<JsonArray>();
  integrations.add("zigbee");
  integrations.add("hue");

  doc["shouldPrompt"] = shouldPrompt;
  doc["alwaysReport"] = browserAutoReport;

  String json;
  serializeJson(doc, json);
  return json;
}

void usageSaveConsent(bool consent, bool remember) {
  Preferences p;
  p.begin(NVS_NS, false);
  p.putString(NVS_PREV_VER, FIRMWARE_VERSION);
  if (remember) {
    p.putBool(NVS_ALWAYS, consent);
    p.putBool(NVS_NEVER,  !consent);
  }
  p.end();
}

/*
 * Zigbee WLED Bridge - Configuration Storage Implementation
 */

#include "config_store.h"

ConfigStore configStore;

void ConfigStore::begin() {
  load();
}

int ConfigStore::addLight(const char* name, LightType type, const char* wledHost, uint16_t wledPort) {
  if (lightCount >= MAX_LIGHTS) return -1;

  uint8_t idx = lightCount;
  LightConfig& cfg = lights[idx];
  cfg.active = true;
  strlcpy(cfg.name, name, sizeof(cfg.name));
  cfg.type = type;
  strlcpy(cfg.wledHost, wledHost, sizeof(cfg.wledHost));
  cfg.wledPort = wledPort;

  lightCount++;
  return idx;
}

bool ConfigStore::removeLight(uint8_t index) {
  if (index >= lightCount) return false;

  // Shift remaining lights down
  for (uint8_t i = index; i < lightCount - 1; i++) {
    lights[i] = lights[i + 1];
  }
  // Clear last slot
  memset(&lights[lightCount - 1], 0, sizeof(LightConfig));
  lightCount--;
  return true;
}

bool ConfigStore::updateLight(uint8_t index, const LightConfig& cfg) {
  if (index >= lightCount) return false;
  lights[index] = cfg;
  return true;
}

void ConfigStore::save() {
  prefs.begin("zbwled", false);

  prefs.putUChar("lightCount", lightCount);

  for (uint8_t i = 0; i < lightCount; i++) {
    char key[16];

    snprintf(key, sizeof(key), "l%d_name", i);
    prefs.putString(key, lights[i].name);

    snprintf(key, sizeof(key), "l%d_type", i);
    prefs.putUChar(key, static_cast<uint8_t>(lights[i].type));

    snprintf(key, sizeof(key), "l%d_host", i);
    prefs.putString(key, lights[i].wledHost);

    snprintf(key, sizeof(key), "l%d_port", i);
    prefs.putUShort(key, lights[i].wledPort);
  }

  prefs.end();
  ESP_LOGI("Config", "Saved %d lights to NVS", lightCount);
}

void ConfigStore::load() {
  // Open in read-write mode to ensure namespace is created on first boot
  if (!prefs.begin("zbwled", false)) {
    ESP_LOGW("Config", "NVS namespace init failed, starting with empty config");
    lightCount = 0;
    return;
  }

  lightCount = prefs.getUChar("lightCount", 0);
  if (lightCount > MAX_LIGHTS) lightCount = MAX_LIGHTS;

  for (uint8_t i = 0; i < lightCount; i++) {
    char key[16];

    snprintf(key, sizeof(key), "l%d_name", i);
    String name = prefs.getString(key, "Light");
    strlcpy(lights[i].name, name.c_str(), sizeof(lights[i].name));

    snprintf(key, sizeof(key), "l%d_type", i);
    lights[i].type = static_cast<LightType>(prefs.getUChar(key, LIGHT_TYPE_RGB));

    snprintf(key, sizeof(key), "l%d_host", i);
    String host = prefs.getString(key, "");
    strlcpy(lights[i].wledHost, host.c_str(), sizeof(lights[i].wledHost));

    snprintf(key, sizeof(key), "l%d_port", i);
    lights[i].wledPort = prefs.getUShort(key, 80);

    lights[i].active = true;
  }

  prefs.end();
  ESP_LOGI("Config", "Loaded %d lights from NVS", lightCount);
}

void ConfigStore::toJson(JsonDocument& doc) const {
  // Lights
  JsonArray arr = doc["lights"].to<JsonArray>();
  for (uint8_t i = 0; i < lightCount; i++) {
    JsonObject light = arr.add<JsonObject>();
    light["name"] = lights[i].name;
    light["type"] = (lights[i].type == LIGHT_TYPE_RGBW) ? "RGBW" : "RGB";
    light["wledHost"] = lights[i].wledHost;
    light["wledPort"] = lights[i].wledPort;
  }
}

bool ConfigStore::fromJson(const JsonDocument& doc) {
  // ArduinoJson v7: check for lights array
  JsonArrayConst arr = doc["lights"].as<JsonArrayConst>();
  if (arr.isNull()) {
    ESP_LOGE("Config", "fromJson: 'lights' key missing or not an array");
    JsonObjectConst root = doc.as<JsonObjectConst>();
    if (!root.isNull()) {
      for (JsonPairConst kv : root) {
        ESP_LOGE("Config", "  key: '%s'", kv.key().c_str());
      }
    } else {
      ESP_LOGE("Config", "  doc is not an object either");
    }
    return false;
  }

  uint8_t count = 0;

  for (JsonObjectConst light : arr) {
    if (count >= MAX_LIGHTS) break;

    LightConfig& cfg = lights[count];
    cfg.active = true;

    const char* name = light["name"] | "Light";
    strlcpy(cfg.name, name, sizeof(cfg.name));

    const char* type = light["type"] | "RGB";
    cfg.type = (strcmp(type, "RGBW") == 0) ? LIGHT_TYPE_RGBW : LIGHT_TYPE_RGB;

    const char* host = light["wledHost"] | "";
    strlcpy(cfg.wledHost, host, sizeof(cfg.wledHost));

    cfg.wledPort = light["wledPort"] | 80;

    count++;
  }

  // Clear remaining slots
  for (uint8_t i = count; i < lightCount; i++) {
    memset(&lights[i], 0, sizeof(LightConfig));
  }

  lightCount = count;
  ESP_LOGI("Config", "fromJson: parsed %d lights", count);
  return true;
}

void ConfigStore::factoryReset() {
  prefs.begin("zbwled", false);
  prefs.clear();
  prefs.end();

  lightCount = 0;
  memset(lights, 0, sizeof(lights));

  ESP_LOGI("Config", "Factory reset complete");
}

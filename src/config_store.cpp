/*
 * Zigbee DMX Bridge - Configuration Storage Implementation
 */

#include "config_store.h"

ConfigStore configStore;

void ConfigStore::begin() {
  load();
}

int ConfigStore::addLight(const char* name, LightType type, uint16_t dmxAddr) {
  if (lightCount >= MAX_LIGHTS) return -1;

  uint8_t idx = lightCount;
  LightConfig& cfg = lights[idx];
  cfg.active = true;
  strlcpy(cfg.name, name, sizeof(cfg.name));
  cfg.type = type;
  cfg.dmxStartAddr = dmxAddr;

  // Default channel mapping: R=0, G=1, B=2, W=3
  cfg.channelMap.red   = 0;
  cfg.channelMap.green = 1;
  cfg.channelMap.blue  = 2;
  cfg.channelMap.white = 3;

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
  prefs.begin("zbdmx", false);

  prefs.putUChar("lightCount", lightCount);

  // Save output config
  prefs.putUChar("outMode", static_cast<uint8_t>(outputCfg.mode));
  prefs.putChar("outTxPin", outputCfg.txPin);
  prefs.putChar("outEnPin", outputCfg.enPin);
  prefs.putUShort("outArtUni", outputCfg.artnetUniverse);

  for (uint8_t i = 0; i < lightCount; i++) {
    char key[16];

    snprintf(key, sizeof(key), "l%d_name", i);
    prefs.putString(key, lights[i].name);

    snprintf(key, sizeof(key), "l%d_type", i);
    prefs.putUChar(key, static_cast<uint8_t>(lights[i].type));

    snprintf(key, sizeof(key), "l%d_addr", i);
    prefs.putUShort(key, lights[i].dmxStartAddr);

    snprintf(key, sizeof(key), "l%d_mapR", i);
    prefs.putUChar(key, lights[i].channelMap.red);

    snprintf(key, sizeof(key), "l%d_mapG", i);
    prefs.putUChar(key, lights[i].channelMap.green);

    snprintf(key, sizeof(key), "l%d_mapB", i);
    prefs.putUChar(key, lights[i].channelMap.blue);

    snprintf(key, sizeof(key), "l%d_mapW", i);
    prefs.putUChar(key, lights[i].channelMap.white);
  }

  prefs.end();
  ESP_LOGI("Config", "Saved %d lights, output mode=%d to NVS", lightCount, outputCfg.mode);
}

void ConfigStore::load() {
  // Open in read-write mode to ensure namespace is created on first boot
  if (!prefs.begin("zbdmx", false)) {
    ESP_LOGW("Config", "NVS namespace init failed, starting with empty config");
    lightCount = 0;
    return;
  }

  lightCount = prefs.getUChar("lightCount", 0);
  if (lightCount > MAX_LIGHTS) lightCount = MAX_LIGHTS;

  // Load output config
  outputCfg.mode = static_cast<OutputMode>(prefs.getUChar("outMode", OUTPUT_MODE_WIRED_DMX));
  outputCfg.txPin = prefs.getChar("outTxPin", 2);
  outputCfg.enPin = prefs.getChar("outEnPin", 4);
  outputCfg.artnetUniverse = prefs.getUShort("outArtUni", 0);

  for (uint8_t i = 0; i < lightCount; i++) {
    char key[16];

    snprintf(key, sizeof(key), "l%d_name", i);
    String name = prefs.getString(key, "Light");
    strlcpy(lights[i].name, name.c_str(), sizeof(lights[i].name));

    snprintf(key, sizeof(key), "l%d_type", i);
    lights[i].type = static_cast<LightType>(prefs.getUChar(key, LIGHT_TYPE_RGB));

    snprintf(key, sizeof(key), "l%d_addr", i);
    lights[i].dmxStartAddr = prefs.getUShort(key, 1);

    snprintf(key, sizeof(key), "l%d_mapR", i);
    lights[i].channelMap.red = prefs.getUChar(key, 0);

    snprintf(key, sizeof(key), "l%d_mapG", i);
    lights[i].channelMap.green = prefs.getUChar(key, 1);

    snprintf(key, sizeof(key), "l%d_mapB", i);
    lights[i].channelMap.blue = prefs.getUChar(key, 2);

    snprintf(key, sizeof(key), "l%d_mapW", i);
    lights[i].channelMap.white = prefs.getUChar(key, 3);

    lights[i].active = true;
  }

  prefs.end();
  ESP_LOGI("Config", "Loaded %d lights, output mode=%d from NVS", lightCount, outputCfg.mode);
}

void ConfigStore::toJson(JsonDocument& doc) const {
  // Output config
  JsonObject output = doc["output"].to<JsonObject>();
  output["mode"] = (outputCfg.mode == OUTPUT_MODE_ARTNET) ? "artnet" : "dmx";
  output["txPin"] = outputCfg.txPin;
  output["enPin"] = outputCfg.enPin;
  output["artnetUniverse"] = outputCfg.artnetUniverse;

  // Lights
  JsonArray arr = doc["lights"].to<JsonArray>();
  for (uint8_t i = 0; i < lightCount; i++) {
    JsonObject light = arr.add<JsonObject>();
    light["name"] = lights[i].name;
    light["type"] = (lights[i].type == LIGHT_TYPE_RGBW) ? "RGBW" : "RGB";
    light["dmxAddr"] = lights[i].dmxStartAddr;

    JsonObject map = light["channelMap"].to<JsonObject>();
    map["r"] = lights[i].channelMap.red;
    map["g"] = lights[i].channelMap.green;
    map["b"] = lights[i].channelMap.blue;
    if (lights[i].type == LIGHT_TYPE_RGBW) {
      map["w"] = lights[i].channelMap.white;
    }
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

  // Parse output config if present
  JsonObjectConst outputObj = doc["output"].as<JsonObjectConst>();
  if (!outputObj.isNull()) {
    const char* mode = outputObj["mode"] | "dmx";
    outputCfg.mode = (strcmp(mode, "artnet") == 0) ? OUTPUT_MODE_ARTNET : OUTPUT_MODE_WIRED_DMX;
    outputCfg.txPin = outputObj["txPin"] | (int)2;
    outputCfg.enPin = outputObj["enPin"] | (int)4;
    outputCfg.artnetUniverse = outputObj["artnetUniverse"] | (int)0;
    ESP_LOGI("Config", "fromJson: output mode=%s txPin=%d enPin=%d artUni=%d",
             mode, outputCfg.txPin, outputCfg.enPin, outputCfg.artnetUniverse);
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

    cfg.dmxStartAddr = light["dmxAddr"] | 1;

    if (light["channelMap"].is<JsonObjectConst>()) {
      JsonObjectConst map = light["channelMap"].as<JsonObjectConst>();
      cfg.channelMap.red   = map["r"] | 0;
      cfg.channelMap.green = map["g"] | 1;
      cfg.channelMap.blue  = map["b"] | 2;
      cfg.channelMap.white = map["w"] | 3;
    } else {
      cfg.channelMap = {0, 1, 2, 3};
    }

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
  prefs.begin("zbdmx", false);
  prefs.clear();
  prefs.end();

  lightCount = 0;
  memset(lights, 0, sizeof(lights));
  outputCfg = OutputConfig();

  ESP_LOGI("Config", "Factory reset complete");
}

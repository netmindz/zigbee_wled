/*
 * Zigbee DMX Bridge - Zigbee Manager Implementation
 *
 * Adapted from the WLED Zigbee RGB Light usermod:
 * https://github.com/netmindz/WLED-MM/tree/zigbee-rgb-light-usermod/usermods/zigbee_rgb_light/
 *
 * Key differences from the WLED version:
 * - Standalone (no WLED dependencies)
 * - Multiple Zigbee endpoints (one per configured light)
 * - Output goes to DMX rather than LED strip
 * - Each light has independent RGB state
 *
 * All critical Hue Bridge pairing requirements are preserved:
 * - End Device mode (Hue rejects Router joins)
 * - Distributed security with ZLL key
 * - app_device_version = 1
 * - Extra on_off attributes (global_scene_control, on_time, off_wait_time)
 * - Minimum join LQI = 0 (PCB antenna workaround)
 * - Raw command handler for off_with_effect and manufacturer scene crash prevention
 */

#include "sdkconfig.h"

#if defined(CONFIG_IDF_TARGET_ESP32C6) || defined(CONFIG_IDF_TARGET_ESP32C5) || defined(CONFIG_SOC_IEEE802154_SUPPORTED)

#include "zigbee_manager.h"

#include "esp_zigbee_core.h"
#include "ha/esp_zigbee_ha_standard.h"
#include "aps/esp_zigbee_aps.h"
#include "esp_coexist.h"
#include "esp_ieee802154.h"
#include "esp_coex_i154.h"

// ZBOSS internal headers for raw command handler
extern "C" {
  struct zb_af_endpoint_desc_s;
  typedef struct zb_af_endpoint_desc_s zb_af_endpoint_desc_t;
  #include "zboss_api_buf.h"
  #include "zcl/zb_zcl_common.h"
  #include "zcl/zb_zcl_color_control.h"
  #include "zcl/zb_zcl_level_control.h"

  void zb_zdo_pim_set_long_poll_interval(zb_time_t ms);
  void zb_zdo_pim_toggle_turbo_poll_retry_feature(zb_bool_t enable);
  zb_bool_t zb_zcl_send_default_handler(zb_uint8_t param,
    const zb_zcl_parsed_hdr_t *cmd_info, zb_zcl_status_t status);
}

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#ifndef ZIGBEE_ENDPOINT_BASE
  #define ZIGBEE_ENDPOINT_BASE 10
#endif

#ifndef ZIGBEE_TASK_STACK_SIZE
  #define ZIGBEE_TASK_STACK_SIZE 16384
#endif

#ifndef ZIGBEE_TASK_PRIORITY
  #define ZIGBEE_TASK_PRIORITY 5
#endif

// ---- Module state ----
static bool zbInitDone    = false;
static bool zbStarted     = false;
static volatile bool zbPaired = false;
static TaskHandle_t zbTaskHandle = nullptr;
static SemaphoreHandle_t zbStateMutex = nullptr;
static char eui64Str[24] = {};
static bool reconfigPending = false;

// Light states (written by Zigbee task, read by main loop for DMX)
static LightState lightStates[MAX_LIGHTS] = {};

// ZLL distributed link key (well-known, used by Hue bridge)
static constexpr uint8_t zll_distributed_key[16] = {
  0x81, 0x42, 0x86, 0x86, 0x5D, 0xC1, 0xC8, 0xB2,
  0xC8, 0xCB, 0xC5, 0x2E, 0x5D, 0x65, 0xD1, 0xB8
};

// Forward declarations
static void zbStartCommissioning(uint8_t mode_mask);
static void zigbeeTask(void *pvParameters);
bool zb_raw_command_handler(uint8_t bufid);

// ---- CIE 1931 XY <-> RGB conversions ----

static void xyToRGB(uint16_t zclX, uint16_t zclY,
                    uint8_t &r, uint8_t &g, uint8_t &b) {
  float x = static_cast<float>(zclX) / 65279.0f;
  float y = static_cast<float>(zclY) / 65279.0f;
  if (y < 0.001f) y = 0.001f;

  float Y = 1.0f;
  float X = (Y / y) * x;
  float Z = (Y / y) * (1.0f - x - y);

  float rf = X * 3.2404542f + Y * -1.5371385f + Z * -0.4985314f;
  float gf = X * -0.9692660f + Y * 1.8760108f + Z * 0.0415560f;
  float bf = X * 0.0556434f + Y * -0.2040259f + Z * 1.0572252f;

  auto clamp01 = [](float v) -> float { return v < 0.0f ? 0.0f : (v > 1.0f ? 1.0f : v); };
  rf = clamp01(rf); gf = clamp01(gf); bf = clamp01(bf);

  auto reverseGamma = [](float v) -> float {
    return v <= 0.0031308f ? 12.92f * v : 1.055f * powf(v, 1.0f / 2.4f) - 0.055f;
  };
  rf = reverseGamma(rf); gf = reverseGamma(gf); bf = reverseGamma(bf);

  float maxC = rf;
  if (gf > maxC) maxC = gf;
  if (bf > maxC) maxC = bf;
  if (maxC > 0.001f) {
    float scale = 255.0f / maxC;
    r = static_cast<uint8_t>(rf * scale + 0.5f);
    g = static_cast<uint8_t>(gf * scale + 0.5f);
    b = static_cast<uint8_t>(bf * scale + 0.5f);
  } else {
    r = g = b = 0;
  }
}

static void rgbToXY(uint8_t r, uint8_t g, uint8_t b,
                    uint16_t &zclX, uint16_t &zclY) {
  auto applyGamma = [](float v) -> float {
    v /= 255.0f;
    return v <= 0.04045f ? v / 12.92f : powf((v + 0.055f) / 1.055f, 2.4f);
  };
  float rf = applyGamma(r); float gf = applyGamma(g); float bf = applyGamma(b);

  float X = rf * 0.4124564f + gf * 0.3575761f + bf * 0.1804375f;
  float Y = rf * 0.2126729f + gf * 0.7151522f + bf * 0.0721750f;
  float Z = rf * 0.0193339f + gf * 0.1191920f + bf * 0.9503041f;

  float sum = X + Y + Z;
  if (sum < 0.001f) {
    zclX = static_cast<uint16_t>(0.3127f * 65279.0f + 0.5f);
    zclY = static_cast<uint16_t>(0.3290f * 65279.0f + 0.5f);
    return;
  }
  zclX = static_cast<uint16_t>((X / sum) * 65279.0f + 0.5f);
  zclY = static_cast<uint16_t>((Y / sum) * 65279.0f + 0.5f);
}

// ---- Helpers: endpoint index from Zigbee endpoint number ----
static int endpointToIndex(uint8_t endpoint) {
  int idx = endpoint - ZIGBEE_ENDPOINT_BASE;
  if (idx < 0 || idx >= MAX_LIGHTS) return -1;
  if (idx >= configStore.getLightCount()) return -1;
  return idx;
}

// ---- Update light state from Zigbee command ----
static void updateLightState(uint8_t endpoint, bool power, uint8_t bri,
                              uint16_t colorX, uint16_t colorY, bool useXY) {
  int idx = endpointToIndex(endpoint);
  if (idx < 0) return;

  if (zbStateMutex && xSemaphoreTake(zbStateMutex, pdMS_TO_TICKS(10)) == pdTRUE) {
    lightStates[idx].powerOn = power;
    lightStates[idx].brightness = bri;
    if (useXY) {
      uint8_t r, g, b;
      xyToRGB(colorX, colorY, r, g, b);
      lightStates[idx].red = r;
      lightStates[idx].green = g;
      lightStates[idx].blue = b;
    }
    xSemaphoreGive(zbStateMutex);
  }
}

static void updateLightStateHS(uint8_t endpoint, bool power, uint8_t bri,
                                uint8_t hue, uint8_t sat) {
  int idx = endpointToIndex(endpoint);
  if (idx < 0) return;

  // Convert ZCL hue (0-254) and saturation (0-254) to RGB
  // Hue: 0-254 maps to 0-360 degrees
  // Saturation: 0-254 maps to 0-255
  float h = (static_cast<float>(hue) / 254.0f) * 360.0f;
  float s = static_cast<float>(sat) / 254.0f;
  float v = 1.0f;  // brightness handled separately

  // HSV to RGB
  float c = v * s;
  float x_val = c * (1.0f - fabsf(fmodf(h / 60.0f, 2.0f) - 1.0f));
  float m = v - c;
  float rf, gf, bf;

  if (h < 60)       { rf = c;     gf = x_val; bf = 0; }
  else if (h < 120) { rf = x_val; gf = c;     bf = 0; }
  else if (h < 180) { rf = 0;     gf = c;     bf = x_val; }
  else if (h < 240) { rf = 0;     gf = x_val; bf = c; }
  else if (h < 300) { rf = x_val; gf = 0;     bf = c; }
  else              { rf = c;     gf = 0;     bf = x_val; }

  if (zbStateMutex && xSemaphoreTake(zbStateMutex, pdMS_TO_TICKS(10)) == pdTRUE) {
    lightStates[idx].powerOn = power;
    lightStates[idx].brightness = bri;
    lightStates[idx].red   = static_cast<uint8_t>((rf + m) * 255.0f);
    lightStates[idx].green = static_cast<uint8_t>((gf + m) * 255.0f);
    lightStates[idx].blue  = static_cast<uint8_t>((bf + m) * 255.0f);
    xSemaphoreGive(zbStateMutex);
  }
}

// ---- Read current attribute values for an endpoint ----
static void readCurrentOnOffLevel(uint8_t endpoint, bool &power, uint8_t &level) {
  power = true;
  level = 254;
  esp_zb_zcl_attr_t *attr;
  attr = esp_zb_zcl_get_attribute(endpoint, ESP_ZB_ZCL_CLUSTER_ID_ON_OFF,
    ESP_ZB_ZCL_CLUSTER_SERVER_ROLE, ESP_ZB_ZCL_ATTR_ON_OFF_ON_OFF_ID);
  if (attr && attr->data_p) power = *(bool *)attr->data_p;

  attr = esp_zb_zcl_get_attribute(endpoint, ESP_ZB_ZCL_CLUSTER_ID_LEVEL_CONTROL,
    ESP_ZB_ZCL_CLUSTER_SERVER_ROLE, ESP_ZB_ZCL_ATTR_LEVEL_CONTROL_CURRENT_LEVEL_ID);
  if (attr && attr->data_p) level = *(uint8_t *)attr->data_p;
}

static void readCurrentColorXY(uint8_t endpoint, uint16_t &colorX, uint16_t &colorY) {
  colorX = 0x616B;
  colorY = 0x607D;
  esp_zb_zcl_attr_t *attr;
  attr = esp_zb_zcl_get_attribute(endpoint, ESP_ZB_ZCL_CLUSTER_ID_COLOR_CONTROL,
    ESP_ZB_ZCL_CLUSTER_SERVER_ROLE, ESP_ZB_ZCL_ATTR_COLOR_CONTROL_CURRENT_X_ID);
  if (attr && attr->data_p) colorX = *(uint16_t *)attr->data_p;

  attr = esp_zb_zcl_get_attribute(endpoint, ESP_ZB_ZCL_CLUSTER_ID_COLOR_CONTROL,
    ESP_ZB_ZCL_CLUSTER_SERVER_ROLE, ESP_ZB_ZCL_ATTR_COLOR_CONTROL_CURRENT_Y_ID);
  if (attr && attr->data_p) colorY = *(uint16_t *)attr->data_p;
}

// ---- Signal handler (called by Zigbee stack) ----
static void zbStartCommissioning(uint8_t mode_mask) {
  esp_zb_bdb_start_top_level_commissioning(mode_mask);
}

extern "C" void esp_zb_app_signal_handler(esp_zb_app_signal_t *signal_struct) {
  uint32_t *p_sg_p     = signal_struct->p_app_signal;
  esp_err_t err_status = signal_struct->esp_err_status;
  esp_zb_app_signal_type_t sig_type = (esp_zb_app_signal_type_t)*p_sg_p;

  switch (sig_type) {
    case ESP_ZB_ZDO_SIGNAL_PRODUCTION_CONFIG_READY:
      ESP_LOGD("ZB", "Production config: %s",
               (err_status == ESP_OK) ? "loaded" : "not found (normal)");
      break;

    case ESP_ZB_ZDO_SIGNAL_SKIP_STARTUP:
      ESP_LOGI("ZB", "Initialize Zigbee stack");
      esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_INITIALIZATION);
      break;

    case ESP_ZB_BDB_SIGNAL_DEVICE_FIRST_START:
    case ESP_ZB_BDB_SIGNAL_DEVICE_REBOOT: {
      // Read EUI64
      esp_zb_ieee_addr_t eui64;
      esp_zb_get_long_address(eui64);
      snprintf(eui64Str, sizeof(eui64Str),
               "%02X:%02X:%02X:%02X:%02X:%02X:%02X:%02X",
               eui64[7], eui64[6], eui64[5], eui64[4],
               eui64[3], eui64[2], eui64[1], eui64[0]);
      ESP_LOGI("ZB", "EUI64: %s", eui64Str);

      if (err_status == ESP_OK) {
        if (esp_zb_bdb_is_factory_new()) {
          ESP_LOGI("ZB", "Starting network steering");
          esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
        } else {
          ESP_LOGI("ZB", "Device rebooted (already commissioned)");
          zbPaired = true;
        }
      } else {
        ESP_LOGW("ZB", "BDB init failed (0x%x), retrying...", err_status);
        esp_zb_scheduler_alarm(zbStartCommissioning,
                               ESP_ZB_BDB_MODE_INITIALIZATION, 1000);
      }
      break;
    }

    case ESP_ZB_BDB_SIGNAL_STEERING:
      if (err_status == ESP_OK) {
        esp_zb_ieee_addr_t epid;
        esp_zb_get_extended_pan_id(epid);
        ESP_LOGI("ZB", "Joined network! PAN: 0x%04hx, Ch: %d, Short: 0x%04hx",
                 esp_zb_get_pan_id(), esp_zb_get_current_channel(),
                 esp_zb_get_short_address());
        zbPaired = true;
      } else {
        ESP_LOGI("ZB", "Steering failed (0x%x), retrying in 5s", err_status);
        esp_zb_scheduler_alarm(zbStartCommissioning,
                               ESP_ZB_BDB_MODE_NETWORK_STEERING, 5000);
      }
      break;

    default:
      ESP_LOGD("ZB", "Signal: 0x%x, status: 0x%x", sig_type, err_status);
      break;
  }
}

// ---- Attribute set callback ----
static esp_err_t zb_action_handler(esp_zb_core_action_callback_id_t callback_id,
                                    const void *message) {
  if (callback_id != ESP_ZB_CORE_SET_ATTR_VALUE_CB_ID) return ESP_OK;

  auto *msg = static_cast<const esp_zb_zcl_set_attr_value_message_t *>(message);
  if (!msg || msg->info.status != ESP_ZB_ZCL_STATUS_SUCCESS) return ESP_ERR_INVALID_ARG;

  uint8_t endpoint = msg->info.dst_endpoint;
  uint16_t cluster = msg->info.cluster;
  uint16_t attrId  = msg->attribute.id;

  int idx = endpointToIndex(endpoint);
  if (idx < 0) return ESP_OK;

  ESP_LOGD("ZB", "Attr set: ep=%d cl=0x%04x attr=0x%04x", endpoint, cluster, attrId);

  if (zbStateMutex && xSemaphoreTake(zbStateMutex, pdMS_TO_TICKS(10)) == pdTRUE) {
    switch (cluster) {
      case ESP_ZB_ZCL_CLUSTER_ID_ON_OFF:
        if (attrId == ESP_ZB_ZCL_ATTR_ON_OFF_ON_OFF_ID) {
          lightStates[idx].powerOn = *(const bool *)msg->attribute.data.value;
        }
        break;
      case ESP_ZB_ZCL_CLUSTER_ID_LEVEL_CONTROL:
        if (attrId == ESP_ZB_ZCL_ATTR_LEVEL_CONTROL_CURRENT_LEVEL_ID) {
          lightStates[idx].brightness = *(const uint8_t *)msg->attribute.data.value;
        }
        break;
      case ESP_ZB_ZCL_CLUSTER_ID_COLOR_CONTROL:
        if (attrId == ESP_ZB_ZCL_ATTR_COLOR_CONTROL_CURRENT_X_ID ||
            attrId == ESP_ZB_ZCL_ATTR_COLOR_CONTROL_CURRENT_Y_ID) {
          // Read both X and Y from attribute cache
          uint16_t x, y;
          readCurrentColorXY(endpoint, x, y);
          uint8_t r, g, b;
          xyToRGB(x, y, r, g, b);
          lightStates[idx].red = r;
          lightStates[idx].green = g;
          lightStates[idx].blue = b;
        }
        break;
    }
    xSemaphoreGive(zbStateMutex);
  }
  return ESP_OK;
}

// ---- Raw ZCL command handler ----
// Handles commands that ZBOSS ignores or that crash it.
bool zb_raw_command_handler(uint8_t bufid) {
  zb_zcl_parsed_hdr_t *cmd_info = ZB_BUF_GET_PARAM(bufid, zb_zcl_parsed_hdr_t);
  uint8_t endpoint = cmd_info->addr_data.common_data.dst_endpoint;

  // Issue #681: Hue manufacturer-specific scene commands crash ZBOSS
  if (cmd_info->cluster_id == ESP_ZB_ZCL_CLUSTER_ID_SCENES &&
      cmd_info->is_manuf_specific &&
      cmd_info->manuf_specific == 0x100b) {
    ESP_LOGD("ZB", "Intercepted Hue scene cmd 0x%02x on ep %d", cmd_info->cmd_id, endpoint);
    zb_zcl_send_default_handler(bufid, cmd_info, ZB_ZCL_STATUS_FAIL);
    return true;
  }

  // ---- On/Off cluster (0x0006) ----
  if (cmd_info->cluster_id == ESP_ZB_ZCL_CLUSTER_ID_ON_OFF &&
      !cmd_info->is_common_command) {

    bool newPower;
    bool handled = false;

    if (cmd_info->cmd_id == 0x40) {
      // Off with effect — ZBOSS silently ignores (issue #519)
      newPower = false;
      handled = true;
    } else if (cmd_info->cmd_id == 0x00) {
      newPower = false;
    } else if (cmd_info->cmd_id == 0x01) {
      newPower = true;
    } else if (cmd_info->cmd_id == 0x02) {
      bool curPower; uint8_t dummy;
      readCurrentOnOffLevel(endpoint, curPower, dummy);
      newPower = !curPower;
    } else {
      return false;
    }

    bool curPower2; uint8_t curLevel;
    readCurrentOnOffLevel(endpoint, curPower2, curLevel);
    uint16_t curX, curY;
    readCurrentColorXY(endpoint, curX, curY);

    updateLightState(endpoint, newPower, curLevel, curX, curY, true);

    if (handled) {
      bool attrVal = newPower;
      esp_zb_zcl_set_attribute_val(endpoint,
        ESP_ZB_ZCL_CLUSTER_ID_ON_OFF, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
        ESP_ZB_ZCL_ATTR_ON_OFF_ON_OFF_ID, &attrVal, false);
      zb_zcl_send_default_handler(bufid, cmd_info, ZB_ZCL_STATUS_SUCCESS);
      return true;
    }
    return false;
  }

  // ---- Level Control cluster (0x0008) ----
  if (cmd_info->cluster_id == ESP_ZB_ZCL_CLUSTER_ID_LEVEL_CONTROL &&
      !cmd_info->is_common_command &&
      (cmd_info->cmd_id == 0x00 || cmd_info->cmd_id == 0x04)) {

    uint8_t *payload = (uint8_t *)zb_buf_begin(bufid);
    zb_uint_t payload_len = zb_buf_len(bufid);
    if (payload_len < 3) return false;

    uint8_t level = payload[0];
    bool newPower;
    if (cmd_info->cmd_id == 0x04) {
      newPower = (level > 0);
    } else {
      uint8_t dummy;
      readCurrentOnOffLevel(endpoint, newPower, dummy);
    }

    uint16_t curX, curY;
    readCurrentColorXY(endpoint, curX, curY);
    updateLightState(endpoint, newPower, level, curX, curY, true);

    return false;
  }

  // ---- Color Control: move_to_color (0x07) ----
  if (cmd_info->cluster_id == ESP_ZB_ZCL_CLUSTER_ID_COLOR_CONTROL &&
      !cmd_info->is_common_command && cmd_info->cmd_id == 0x07) {

    uint8_t *payload = (uint8_t *)zb_buf_begin(bufid);
    zb_uint_t payload_len = zb_buf_len(bufid);
    if (payload_len < 6) return false;

    uint16_t colorX = payload[0] | (payload[1] << 8);
    uint16_t colorY = payload[2] | (payload[3] << 8);

    bool curPower; uint8_t curLevel;
    readCurrentOnOffLevel(endpoint, curPower, curLevel);
    updateLightState(endpoint, curPower, curLevel, colorX, colorY, true);

    return false;
  }

  // ---- Color Control: move_to_hue_saturation (0x06) ----
  if (cmd_info->cluster_id == ESP_ZB_ZCL_CLUSTER_ID_COLOR_CONTROL &&
      !cmd_info->is_common_command && cmd_info->cmd_id == 0x06) {

    uint8_t *payload = (uint8_t *)zb_buf_begin(bufid);
    zb_uint_t payload_len = zb_buf_len(bufid);
    if (payload_len < 4) return false;

    uint8_t hue = payload[0];
    uint8_t sat = payload[1];

    bool curPower; uint8_t curLevel;
    readCurrentOnOffLevel(endpoint, curPower, curLevel);
    updateLightStateHS(endpoint, curPower, curLevel, hue, sat);

    return false;
  }

  // ---- Color Control: move_to_hue (0x00) ----
  if (cmd_info->cluster_id == ESP_ZB_ZCL_CLUSTER_ID_COLOR_CONTROL &&
      !cmd_info->is_common_command && cmd_info->cmd_id == 0x00) {

    uint8_t *payload = (uint8_t *)zb_buf_begin(bufid);
    zb_uint_t payload_len = zb_buf_len(bufid);
    if (payload_len < 4) return false;

    uint8_t hue = payload[0];

    bool curPower; uint8_t curLevel;
    readCurrentOnOffLevel(endpoint, curPower, curLevel);

    uint8_t curSat = 254;
    esp_zb_zcl_attr_t *attr = esp_zb_zcl_get_attribute(endpoint,
      ESP_ZB_ZCL_CLUSTER_ID_COLOR_CONTROL, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
      ESP_ZB_ZCL_ATTR_COLOR_CONTROL_CURRENT_SATURATION_ID);
    if (attr && attr->data_p) curSat = *(uint8_t *)attr->data_p;

    updateLightStateHS(endpoint, curPower, curLevel, hue, curSat);
    return false;
  }

  // ---- Color Control: move_to_saturation (0x03) ----
  if (cmd_info->cluster_id == ESP_ZB_ZCL_CLUSTER_ID_COLOR_CONTROL &&
      !cmd_info->is_common_command && cmd_info->cmd_id == 0x03) {

    uint8_t *payload = (uint8_t *)zb_buf_begin(bufid);
    zb_uint_t payload_len = zb_buf_len(bufid);
    if (payload_len < 3) return false;

    uint8_t sat = payload[0];

    bool curPower; uint8_t curLevel;
    readCurrentOnOffLevel(endpoint, curPower, curLevel);

    uint8_t curHue = 0;
    esp_zb_zcl_attr_t *attr = esp_zb_zcl_get_attribute(endpoint,
      ESP_ZB_ZCL_CLUSTER_ID_COLOR_CONTROL, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
      ESP_ZB_ZCL_ATTR_COLOR_CONTROL_CURRENT_HUE_ID);
    if (attr && attr->data_p) curHue = *(uint8_t *)attr->data_p;

    updateLightStateHS(endpoint, curPower, curLevel, curHue, sat);
    return false;
  }

  return false;
}

// ---- Create one light endpoint with all required clusters ----
static void createLightEndpoint(esp_zb_ep_list_t *ep_list, uint8_t endpoint, const char* name) {
  esp_zb_color_dimmable_light_cfg_t light_cfg = ESP_ZB_DEFAULT_COLOR_DIMMABLE_LIGHT_CONFIG();
  light_cfg.basic_cfg.power_source = ESP_ZB_ZCL_BASIC_POWER_SOURCE_DC_SOURCE;
  light_cfg.color_cfg.color_capabilities = 0x0009;  // HS + XY

  esp_zb_cluster_list_t *cluster_list = esp_zb_color_dimmable_light_clusters_create(&light_cfg);

  // Add extra on_off attributes required by Hue
  esp_zb_attribute_list_t *on_off_cluster = esp_zb_cluster_list_get_cluster(
    cluster_list, ESP_ZB_ZCL_CLUSTER_ID_ON_OFF, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);
  if (on_off_cluster) {
    bool gsc = ESP_ZB_ZCL_ON_OFF_GLOBAL_SCENE_CONTROL_DEFAULT_VALUE;
    uint16_t on_time = ESP_ZB_ZCL_ON_OFF_ON_TIME_DEFAULT_VALUE;
    uint16_t off_wait = ESP_ZB_ZCL_ON_OFF_OFF_WAIT_TIME_DEFAULT_VALUE;
    esp_zb_on_off_cluster_add_attr(on_off_cluster, ESP_ZB_ZCL_ATTR_ON_OFF_GLOBAL_SCENE_CONTROL, &gsc);
    esp_zb_on_off_cluster_add_attr(on_off_cluster, ESP_ZB_ZCL_ATTR_ON_OFF_ON_TIME, &on_time);
    esp_zb_on_off_cluster_add_attr(on_off_cluster, ESP_ZB_ZCL_ATTR_ON_OFF_OFF_WAIT_TIME, &off_wait);
  }

  // Set manufacturer info on basic cluster
  esp_zb_attribute_list_t *basic_cluster = esp_zb_cluster_list_get_cluster(
    cluster_list, ESP_ZB_ZCL_CLUSTER_ID_BASIC, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);
  if (basic_cluster) {
    // ZCL string format: first byte = length
    char manuf[] = "\x0A" "ZigbeeDMX";

    // Build model identifier from name
    char model[34];
    uint8_t nameLen = strlen(name);
    if (nameLen > 32) nameLen = 32;
    model[0] = nameLen;
    memcpy(&model[1], name, nameLen);
    model[nameLen + 1] = '\0';

    char sw[] = "\x05" "0.1.0";
    uint8_t app_ver = 1, stack_ver = 1, hw_ver = 1;

    esp_zb_basic_cluster_add_attr(basic_cluster, ESP_ZB_ZCL_ATTR_BASIC_MANUFACTURER_NAME_ID, manuf);
    esp_zb_basic_cluster_add_attr(basic_cluster, ESP_ZB_ZCL_ATTR_BASIC_MODEL_IDENTIFIER_ID, model);
    esp_zb_basic_cluster_add_attr(basic_cluster, ESP_ZB_ZCL_ATTR_BASIC_SW_BUILD_ID, sw);
    esp_zb_basic_cluster_add_attr(basic_cluster, ESP_ZB_ZCL_ATTR_BASIC_APPLICATION_VERSION_ID, &app_ver);
    esp_zb_basic_cluster_add_attr(basic_cluster, ESP_ZB_ZCL_ATTR_BASIC_STACK_VERSION_ID, &stack_ver);
    esp_zb_basic_cluster_add_attr(basic_cluster, ESP_ZB_ZCL_ATTR_BASIC_HW_VERSION_ID, &hw_ver);
  }

  // Create endpoint with app_device_version = 1 (required by Hue)
  esp_zb_endpoint_config_t endpoint_config = {
    .endpoint = endpoint,
    .app_profile_id = ESP_ZB_AF_HA_PROFILE_ID,
    .app_device_id = ESP_ZB_HA_COLOR_DIMMABLE_LIGHT_DEVICE_ID,
    .app_device_version = 1,
  };
  esp_zb_ep_list_add_ep(ep_list, cluster_list, endpoint_config);

  ESP_LOGI("ZB", "Created endpoint %d: %s", endpoint, name);
}

// ---- Zigbee FreeRTOS task ----
static void zigbeeTask(void *pvParameters) {
  esp_zb_io_buffer_size_set(128);
  esp_zb_scheduler_queue_size_set(128);

  // Init as End Device (Hue rejects Router joins)
  esp_zb_cfg_t zb_nwk_cfg = {};
  zb_nwk_cfg.esp_zb_role = ESP_ZB_DEVICE_TYPE_ED;
  zb_nwk_cfg.install_code_policy = false;
  zb_nwk_cfg.nwk_cfg.zed_cfg.ed_timeout = ESP_ZB_ED_AGING_TIMEOUT_64MIN;
  zb_nwk_cfg.nwk_cfg.zed_cfg.keep_alive = 3000;
  esp_zb_init(&zb_nwk_cfg);

  // Critical: distributed security for Hue bridge
  esp_zb_enable_joining_to_distributed(true);
  esp_zb_secur_TC_standard_distributed_key_set(const_cast<uint8_t *>(zll_distributed_key));
  esp_zb_set_rx_on_when_idle(true);
  esp_zb_secur_network_min_join_lqi_set(0);

  // Create endpoints for all configured lights
  esp_zb_ep_list_t *ep_list = esp_zb_ep_list_create();
  uint8_t count = configStore.getLightCount();

  if (count == 0) {
    // Create at least one default endpoint so the device can join
    createLightEndpoint(ep_list, ZIGBEE_ENDPOINT_BASE, "Default Light");
    ESP_LOGW("ZB", "No lights configured, created default endpoint");
  } else {
    for (uint8_t i = 0; i < count; i++) {
      const LightConfig& cfg = configStore.getLight(i);
      createLightEndpoint(ep_list, ZIGBEE_ENDPOINT_BASE + i, cfg.name);
    }
  }

  esp_zb_device_register(ep_list);

  // Patch reportable attribute access flags
  uint8_t epCount = (count > 0) ? count : 1;
  for (uint8_t i = 0; i < epCount; i++) {
    uint8_t ep = ZIGBEE_ENDPOINT_BASE + i;
    struct { uint16_t cluster; uint16_t attr; } reportable[] = {
      { ESP_ZB_ZCL_CLUSTER_ID_ON_OFF,       ESP_ZB_ZCL_ATTR_ON_OFF_ON_OFF_ID },
      { ESP_ZB_ZCL_CLUSTER_ID_LEVEL_CONTROL, ESP_ZB_ZCL_ATTR_LEVEL_CONTROL_CURRENT_LEVEL_ID },
      { ESP_ZB_ZCL_CLUSTER_ID_COLOR_CONTROL, ESP_ZB_ZCL_ATTR_COLOR_CONTROL_CURRENT_X_ID },
      { ESP_ZB_ZCL_CLUSTER_ID_COLOR_CONTROL, ESP_ZB_ZCL_ATTR_COLOR_CONTROL_CURRENT_Y_ID },
    };
    for (auto &ra : reportable) {
      esp_zb_zcl_attr_t *a = esp_zb_zcl_get_attribute(ep, ra.cluster,
        ESP_ZB_ZCL_CLUSTER_SERVER_ROLE, ra.attr);
      if (a) a->access |= ESP_ZB_ZCL_ATTR_ACCESS_REPORTING;
    }
  }

  esp_zb_core_action_handler_register(zb_action_handler);
  esp_zb_raw_command_handler_register(zb_raw_command_handler);
  esp_zb_set_primary_network_channel_set(ESP_ZB_TRANSCEIVER_ALL_CHANNELS_MASK);

  ESP_ERROR_CHECK(esp_zb_start(false));

  // Configure ED polling
  zb_zdo_pim_set_long_poll_interval(1000);
  zb_zdo_pim_toggle_turbo_poll_retry_feature(ZB_TRUE);

  ESP_LOGI("ZB", "Zigbee stack started with %d endpoint(s)", epCount);

  // Run main loop — never returns
  esp_zb_stack_main_loop();
  vTaskDelete(nullptr);
}

// ---- Public API ----

void zigbeeSetup() {
  // Enable WiFi/802.15.4 coexistence before WiFi starts
#if defined(CONFIG_ESP_COEX_SW_COEXIST_ENABLE) || defined(CONFIG_ESP_COEX_ENABLED)
  esp_err_t coex_err = esp_coex_wifi_i154_enable();
  if (coex_err == ESP_OK) {
    ESP_LOGD("ZB", "WiFi/802.15.4 coexistence enabled");
  } else {
    ESP_LOGE("ZB", "esp_coex_wifi_i154_enable failed: 0x%x", coex_err);
  }

  esp_ieee802154_coex_config_t coex_cfg = {
    .idle    = IEEE802154_IDLE,
    .txrx    = IEEE802154_MIDDLE,
    .txrx_at = IEEE802154_MIDDLE,
  };
  esp_ieee802154_set_coex_config(coex_cfg);
#endif

  zbStateMutex = xSemaphoreCreateMutex();
  if (!zbStateMutex) {
    ESP_LOGE("ZB", "Failed to create mutex");
    return;
  }

  // Initialize default light states
  for (int i = 0; i < MAX_LIGHTS; i++) {
    lightStates[i].powerOn = true;
    lightStates[i].brightness = 254;
    lightStates[i].red = 255;
    lightStates[i].green = 255;
    lightStates[i].blue = 255;
    lightStates[i].white = 0;
  }

  // Configure Zigbee platform
  esp_zb_platform_config_t platform_cfg = {};
  platform_cfg.radio_config.radio_mode          = ZB_RADIO_MODE_NATIVE;
  platform_cfg.host_config.host_connection_mode = ZB_HOST_CONNECTION_MODE_NONE;
  if (esp_zb_platform_config(&platform_cfg) != ESP_OK) {
    ESP_LOGE("ZB", "esp_zb_platform_config failed");
    return;
  }

  zbInitDone = true;
  ESP_LOGI("ZB", "Platform configured, waiting for WiFi");
}

void zigbeeStart() {
  if (!zbInitDone || zbStarted) return;

  // Re-apply coexistence on WiFi connect
#if defined(CONFIG_ESP_COEX_SW_COEXIST_ENABLE) || defined(CONFIG_ESP_COEX_ENABLED)
  esp_coex_wifi_i154_enable();
  esp_ieee802154_coex_config_t coex_cfg = {
    .idle    = IEEE802154_IDLE,
    .txrx    = IEEE802154_MIDDLE,
    .txrx_at = IEEE802154_MIDDLE,
  };
  esp_ieee802154_set_coex_config(coex_cfg);
#endif

  ESP_LOGI("ZB", "Starting Zigbee task");
  if (xTaskCreate(zigbeeTask, "zigbee", ZIGBEE_TASK_STACK_SIZE,
                   nullptr, ZIGBEE_TASK_PRIORITY, &zbTaskHandle) == pdPASS) {
    zbStarted = true;
  } else {
    ESP_LOGE("ZB", "Failed to create Zigbee task");
  }
}

bool zigbeeIsPaired() {
  return zbPaired;
}

const char* zigbeeGetEUI64() {
  return eui64Str;
}

const LightState& zigbeeGetLightState(uint8_t index) {
  static LightState defaultState = {true, 254, 255, 255, 255, 0};
  if (index >= MAX_LIGHTS) return defaultState;

  // Return a snapshot under mutex
  if (zbStateMutex && xSemaphoreTake(zbStateMutex, pdMS_TO_TICKS(5)) == pdTRUE) {
    static LightState snapshot;
    snapshot = lightStates[index];
    xSemaphoreGive(zbStateMutex);
    return snapshot;
  }
  return lightStates[index];  // fallback: no mutex, best effort
}

void zigbeeReportState(uint8_t index, const LightState& state) {
  // TODO: implement WLED→coordinator attribute reporting
  // For now, Zigbee is receive-only (coordinator controls the lights)
}

void zigbeeReconfigure() {
  // Zigbee endpoints are registered at task startup and cannot be
  // dynamically changed without restarting the stack.
  // Set a flag and inform the user a restart is needed.
  reconfigPending = true;
  ESP_LOGW("ZB", "Light config changed. Restart required for Zigbee to pick up changes.");
}

#else
// Stub implementations for non-C6 targets (won't compile Zigbee code)

#include "zigbee_manager.h"

void zigbeeSetup() {
  ESP_LOGE("ZB", "Zigbee requires ESP32-C6 or ESP32-C5!");
}
void zigbeeStart() {}
bool zigbeeIsPaired() { return false; }
const char* zigbeeGetEUI64() { return "N/A"; }
const LightState& zigbeeGetLightState(uint8_t index) {
  static LightState s = {};
  return s;
}
void zigbeeReportState(uint8_t index, const LightState& state) {}
void zigbeeReconfigure() {}

#endif // CONFIG_IDF_TARGET_ESP32C6

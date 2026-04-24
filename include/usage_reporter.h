/*
 * Zigbee WLED Bridge - Usage Reporter
 *
 * Provides device identity and consent tracking for usage reporting to
 * usage.wled.me.  The actual HTTP POST is performed by the browser (web UI)
 * so no outbound HTTP client is needed in the firmware.
 *
 * Public API:
 *   usageGetInfoJson()    — JSON payload for GET /api/usage-info
 *   usageSaveConsent()    — persist consent preference to NVS
 */

#pragma once

#include <Arduino.h>

/**
 * @brief Build the JSON object returned by GET /api/usage-info.
 *
 * Contains every field the browser needs to POST to usage.wled.me, plus
 * two control fields:
 *   "shouldPrompt"  — true when the user should be asked for consent
 *   "alwaysReport"  — true when the user previously opted in permanently
 *
 * @return Serialised JSON string.
 */
String usageGetInfoJson();

/**
 * @brief Persist the user's consent choice to NVS.
 *
 * @param consent  true = user agreed to report; false = user declined.
 * @param remember true = save the choice permanently so the dialog is not
 *                 shown again (sets alwaysReport or neverAsk accordingly).
 */
void usageSaveConsent(bool consent, bool remember);

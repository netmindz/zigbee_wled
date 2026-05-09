/*
 * Zigbee WLED Bridge - HTTP Log Ring Buffer
 *
 * Captures ESP_LOG* output into a 32 KB ring buffer via esp_log_set_vprintf
 * so recent log history can be fetched over HTTP at any time without needing
 * a live serial connection.
 *
 * Call logBufferInit() once, early in setup(), after Serial.begin().
 * All ESP_LOG* calls made after that point are captured.
 *
 * Requires build flags:
 *   -D LOG_LOCAL_LEVEL=ESP_LOG_VERBOSE   (compile-time gate)
 *   -D CONFIG_LOG_MAXIMUM_LEVEL=5        (belt-and-braces)
 *   -D USE_ESP_IDF_LOG                   (routes Arduino log_* through ESP-IDF)
 */

#pragma once

#include <stddef.h>

/** Install the vprintf hook and initialise the ring buffer. */
void logBufferInit();

/**
 * Copy the ring buffer contents (oldest → newest) into @p out.
 * At most @p maxLen-1 bytes are written; always NUL-terminates.
 * @return Number of bytes written (excluding NUL).
 */
size_t logBufferRead(char *out, size_t maxLen);

/** Number of bytes currently stored (0 → LOG_BUF_SIZE). */
size_t logBufferSize();

/** Zero the ring buffer. */
void logBufferClear();

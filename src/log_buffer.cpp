/*
 * Zigbee WLED Bridge - HTTP Log Ring Buffer
 *
 * Hooks into esp_log_set_vprintf to capture all ESP_LOG* output into a 32 KB
 * static ring buffer in internal SRAM.  The hook forwards output to vprintf
 * (UART) unchanged so serial monitoring continues to work.
 *
 * Design constraints:
 *  - No malloc inside the vprintf hook — uses a 512-byte stack scratch buffer
 *    with truncation.  Avoids heap-vs-log reentrancy problems.
 *  - Mutex protects the ring from concurrent task writes.
 *  - va_copy used before every vsnprintf so the original va_list is intact
 *    for the subsequent vprintf (UART forward).
 */

#include "log_buffer.h"

#include <string.h>
#include <stdarg.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <esp_log.h>

// ---- Ring buffer -------------------------------------------------------

static constexpr size_t LOG_BUF_SIZE = 32768;  // 32 KB in internal SRAM

static char              s_buf[LOG_BUF_SIZE];
static size_t            s_head    = 0;      // next write position
static bool              s_wrapped = false;
static SemaphoreHandle_t s_mutex   = nullptr;

// ---- Internal helpers --------------------------------------------------

static void ringWrite(const char *data, size_t len) {
  // Caller must hold s_mutex.
  for (size_t i = 0; i < len; i++) {
    s_buf[s_head] = data[i];
    if (++s_head == LOG_BUF_SIZE) {
      s_head    = 0;
      s_wrapped = true;
    }
  }
}

// ---- vprintf hook ------------------------------------------------------

static int logHook(const char *fmt, va_list args) {
  // Stack scratch — 512 bytes covers virtually all ESP_LOG lines; longer
  // lines are silently truncated in the ring buffer but still fully printed
  // to UART via the vprintf call below.
  char scratch[512];

  va_list args_copy;
  va_copy(args_copy, args);
  int n = vsnprintf(scratch, sizeof(scratch), fmt, args_copy);
  va_end(args_copy);

  if (n > 0 && s_mutex) {
    size_t len = (static_cast<size_t>(n) < sizeof(scratch))
                   ? static_cast<size_t>(n)
                   : sizeof(scratch) - 1;
    if (xSemaphoreTake(s_mutex, 0) == pdTRUE) {  // non-blocking to avoid ISR deadlock
      ringWrite(scratch, len);
      xSemaphoreGive(s_mutex);
    }
  }

  // Forward to UART using the original (unconsumed) va_list.
  return vprintf(fmt, args);
}

// ---- Public API --------------------------------------------------------

void logBufferInit() {
  memset(s_buf, 0, sizeof(s_buf));
  s_head    = 0;
  s_wrapped = false;
  s_mutex   = xSemaphoreCreateMutex();

  // Enable INFO level at runtime (compile-time gate handled by build flags).
  esp_log_level_set("*", ESP_LOG_INFO);

  esp_log_set_vprintf(logHook);
}

size_t logBufferRead(char *out, size_t maxLen) {
  if (!s_mutex || !out || maxLen == 0) {
    if (out && maxLen) out[0] = '\0';
    return 0;
  }

  maxLen--;  // reserve one byte for NUL terminator

  xSemaphoreTake(s_mutex, portMAX_DELAY);

  size_t written = 0;

  if (!s_wrapped) {
    // Linear: data occupies [0, s_head).
    size_t avail = s_head;
    size_t copy  = avail < maxLen ? avail : maxLen;
    memcpy(out, s_buf, copy);
    written = copy;
  } else {
    // Wrapped: oldest data starts at s_head, runs to end, then 0..s_head-1.
    size_t tail  = LOG_BUF_SIZE - s_head;
    size_t copy1 = tail < maxLen ? tail : maxLen;
    memcpy(out, s_buf + s_head, copy1);
    written = copy1;

    if (written < maxLen) {
      size_t rem   = maxLen - written;
      size_t copy2 = s_head < rem ? s_head : rem;
      memcpy(out + written, s_buf, copy2);
      written += copy2;
    }
  }

  xSemaphoreGive(s_mutex);

  out[written] = '\0';
  return written;
}

size_t logBufferSize() {
  if (!s_mutex) return 0;
  xSemaphoreTake(s_mutex, portMAX_DELAY);
  size_t sz = s_wrapped ? LOG_BUF_SIZE : s_head;
  xSemaphoreGive(s_mutex);
  return sz;
}

void logBufferClear() {
  if (!s_mutex) return;
  xSemaphoreTake(s_mutex, portMAX_DELAY);
  memset(s_buf, 0, sizeof(s_buf));
  s_head    = 0;
  s_wrapped = false;
  xSemaphoreGive(s_mutex);
}

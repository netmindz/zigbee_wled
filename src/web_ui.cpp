/*
 * Zigbee WLED Bridge - Web UI & WiFi Manager Implementation
 *
 * Provides:
 * - Captive portal AP for initial WiFi setup
 * - Configuration web UI for light definitions with WLED device discovery
 * - REST API: GET/POST /api/config, POST /api/factory-reset
 * - WLED discovery API: GET /api/wled/discover
 * - Status API: GET /api/status
 * - SSE event stream: GET /api/events (replaces polling for live updates)
 *
 * Uses the ESP-IDF native HTTP server (esp_http_server) for async request
 * handling and SSE support.  The server runs in its own FreeRTOS task
 * so webLoop() no longer needs to pump handleClient().
 */

#include "web_ui.h"
#include "config_store.h"
#include "wled_output.h"
#include "wled_discovery.h"
#include "zigbee_manager.h"

#include <WiFi.h>
#include <DNSServer.h>
#include <esp_http_server.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <Update.h>

static httpd_handle_t httpServer = nullptr;
static DNSServer dnsServer;
static bool apMode = false;
static bool serverStarted = false;

// WiFi credentials stored in NVS
static String wifiSSID;
static String wifiPass;

// ---- HTML served from PROGMEM ----
static const char INDEX_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Zigbee WLED Bridge</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%231a1a2e'/%3E%3Cpath d='M18 4L8 18h6l-2 10 10-14h-6z' fill='%2300d4ff'/%3E%3C/svg%3E">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #1a1a2e; color: #e0e0e0; max-width: 800px; margin: 0 auto; padding: 16px; }
  h1 { color: #00d4ff; margin-bottom: 8px; font-size: 1.4em; }
  h2 { color: #aaa; font-size: 1em; margin-bottom: 16px; font-weight: normal; }
  .card { background: #16213e; border-radius: 8px; padding: 16px; margin-bottom: 12px;
          border: 1px solid #0f3460; }
  .status { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }
  .status-item { background: #0f3460; padding: 8px 16px; border-radius: 6px; }
  .status-item .label { font-size: 0.8em; color: #888; }
  .status-item .value { font-size: 1.1em; color: #00d4ff; }
  .light { display: grid; grid-template-columns: auto 1fr auto; gap: 8px; align-items: center; }
  .light-preview { width: 36px; height: 36px; border-radius: 6px; border: 2px solid #0f3460;
                   background: #000; flex-shrink: 0; }
  .light-info { display: flex; flex-direction: column; gap: 4px; }
  .light-name { font-weight: bold; color: #e0e0e0; }
  .light-detail { font-size: 0.85em; color: #888; }
  .btn { padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer;
         font-size: 0.9em; color: #fff; }
  .btn-primary { background: #00d4ff; color: #1a1a2e; }
  .btn-danger { background: #e74c3c; }
  .btn-sm { padding: 4px 10px; font-size: 0.8em; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  input, select { background: #1a1a2e; color: #e0e0e0; border: 1px solid #0f3460;
                  border-radius: 4px; padding: 6px 10px; font-size: 0.9em; width: 100%; }
  label { font-size: 0.85em; color: #aaa; display: block; margin-bottom: 4px; }
  .form-group { margin-bottom: 10px; }
  .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
           background: rgba(0,0,0,0.7); z-index: 100; justify-content: center; align-items: center; }
  .modal.active { display: flex; }
  .modal-content { background: #16213e; border-radius: 8px; padding: 24px;
                   max-width: 500px; width: 90%; border: 1px solid #0f3460;
                   max-height: 90vh; overflow-y: auto; }
  .modal-title { font-size: 1.1em; margin-bottom: 16px; color: #00d4ff; }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
  .wifi-form { margin-top: 16px; }
  #lightList { min-height: 40px; }
  .empty-state { text-align: center; padding: 24px; color: #666; }
  .actions { display: flex; gap: 8px; margin-top: 16px; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 4px;
         font-size: 0.75em; font-weight: bold; }
  .tag-rgb { background: #2d1f6e; color: #a78bfa; }
  .tag-rgbw { background: #1f4e6e; color: #7bc8fa; }
  .hidden { display: none; }
  .wled-device { background: #0f3460; border-radius: 6px; padding: 12px; margin: 8px 0;
                 cursor: pointer; border: 2px solid transparent; transition: border-color 0.2s; }
  .wled-device:hover { border-color: #00d4ff; }
  .wled-device.selected { border-color: #00d4ff; background: #1a3a6e; }
  .wled-device .dev-name { font-weight: bold; color: #e0e0e0; }
  .wled-device .dev-detail { font-size: 0.85em; color: #888; margin-top: 4px; }
  .discover-status { text-align: center; padding: 12px; color: #888; font-size: 0.9em; }
  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid #888;
             border-top-color: #00d4ff; border-radius: 50%; animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<h1>Zigbee WLED Bridge</h1>
<h2>ESP32-C6 Zigbee-to-WLED Light Controller</h2>

<div class="status" id="statusBar">
  <div class="status-item">
    <div class="label">WiFi</div>
    <div class="value" id="wifiStatus">...</div>
  </div>
  <div class="status-item">
    <div class="label">Zigbee</div>
    <div class="value" id="zbStatus">...</div>
  </div>
  <div class="status-item">
    <div class="label">Lights</div>
    <div class="value" id="lightCountStatus">0</div>
  </div>
</div>

<!-- WiFi setup (shown in AP mode) -->
<div class="card" id="wifiSetup" style="display:none">
  <h3 style="color:#00d4ff;margin-bottom:12px">WiFi Setup</h3>
  <div class="wifi-form">
    <div class="form-group">
      <label>SSID</label>
      <input type="text" id="wifiSSID" placeholder="Your WiFi network name">
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" id="wifiPass" placeholder="WiFi password">
    </div>
    <button class="btn btn-primary" onclick="saveWifi()">Connect</button>
  </div>
</div>

<!-- Lights list -->
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <h3 style="color:#00d4ff">Lights</h3>
    <button class="btn btn-primary btn-sm" onclick="showAddLight()">+ Add Light</button>
  </div>
  <div id="lightList">
    <div class="empty-state" id="loadingState">Loading configuration...</div>
  </div>
</div>

<!-- Actions -->
<div class="actions">
  <button class="btn btn-sm" style="background:#0f3460" onclick="restartDevice()">Restart Device</button>
  <button class="btn btn-danger btn-sm" onclick="factoryReset()">Factory Reset</button>
</div>

<!-- OTA Firmware Update -->
<div class="card" style="margin-top:12px">
  <h3 style="color:#00d4ff;margin-bottom:12px">Firmware Update</h3>
  <form id="otaForm" method="POST" action="/api/ota" enctype="multipart/form-data">
    <div class="form-group">
      <label>Select firmware binary (.bin)</label>
      <input type="file" name="firmware" accept=".bin" id="otaFile">
    </div>
    <button class="btn btn-primary btn-sm" type="button" onclick="uploadOTA()">Upload &amp; Flash</button>
    <div id="otaProgress" style="margin-top:8px;display:none">
      <div style="background:#0f3460;border-radius:4px;overflow:hidden;height:24px">
        <div id="otaBar" style="background:#00d4ff;height:100%;width:0%;transition:width 0.3s"></div>
      </div>
      <div id="otaStatus" style="font-size:0.85em;color:#aaa;margin-top:4px">Uploading...</div>
    </div>
  </form>
</div>

<!-- Add/Edit Light Modal -->
<div class="modal" id="lightModal">
  <div class="modal-content">
    <div class="modal-title" id="modalTitle">Add Light</div>
    <input type="hidden" id="editIndex" value="-1">
    <div class="form-group">
      <label>Name</label>
      <input type="text" id="lightName" placeholder="e.g. Living Room Strip">
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Type</label>
        <select id="lightType">
          <option value="RGB">RGB</option>
          <option value="RGBW">RGBW</option>
        </select>
      </div>
      <div class="form-group">
        <label>WLED Port</label>
        <input type="number" id="wledPort" min="1" max="65535" value="80">
      </div>
    </div>
    <div class="form-group">
      <label>WLED Device IP / Hostname</label>
      <input type="text" id="wledHost" placeholder="e.g. 192.168.1.50 or wled-abcdef.local">
    </div>

    <!-- WLED Discovery section -->
    <div style="margin-top:12px;padding-top:12px;border-top:1px solid #0f3460">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <label style="margin:0">Or discover WLED devices on network:</label>
        <button class="btn btn-sm" style="background:#0f3460" onclick="discoverWled()" id="discoverBtn">Scan</button>
      </div>
      <div id="discoveryResults" style="max-height:200px;overflow-y:auto">
        <div class="discover-status" id="discoveryStatus">Click "Scan" to find WLED devices</div>
      </div>
    </div>

    <div class="modal-actions">
      <button class="btn" style="background:#444" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="saveLight()">Save</button>
    </div>
  </div>
</div>

<script>
let config = { lights: [] };
let isApMode = false;

async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('wifiStatus').textContent = s.wifi || 'Unknown';
    document.getElementById('zbStatus').textContent = s.zigbee || 'Unknown';
    isApMode = s.apMode || false;
    document.getElementById('wifiSetup').style.display = isApMode ? 'block' : 'none';
  } catch(e) {
    document.getElementById('wifiStatus').textContent = 'AP Mode';
    document.getElementById('wifiSetup').style.display = 'block';
    isApMode = true;
  }
}

async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    config = await r.json();
    renderLights();
  } catch(e) {
    console.error('Failed to load config:', e);
  }
}

async function loadLightStates() {
  try {
    const r = await fetch('/api/lights/state');
    const data = await r.json();
    const lights = data.lights || [];
    for (let i = 0; i < lights.length; i++) {
      const el = document.getElementById('preview-' + i);
      if (!el) continue;
      const l = lights[i];
      if (l.on) {
        const pw = l.w || 0;
        const pr = Math.min(255, l.r + pw);
        const pg = Math.min(255, l.g + pw);
        const pb = Math.min(255, l.b + pw);
        el.style.background = 'rgb(' + pr + ',' + pg + ',' + pb + ')';
        el.style.boxShadow = '0 0 8px rgba(' + pr + ',' + pg + ',' + pb + ',0.5)';
      } else {
        el.style.background = '#111';
        el.style.boxShadow = 'none';
      }
    }
  } catch(e) { /* ignore */ }
}

function renderLights() {
  const list = document.getElementById('lightList');
  document.getElementById('lightCountStatus').textContent = config.lights.length;

  if (config.lights.length === 0) {
    list.innerHTML = '<div class="empty-state" id="emptyState">No lights configured. Add one to get started.</div>';
    return;
  }

  list.innerHTML = config.lights.map((l, i) => {
    const tagClass = l.type === 'RGBW' ? 'tag-rgbw' : 'tag-rgb';
    return `<div class="card light">
      <div class="light-preview" id="preview-${i}"></div>
      <div class="light-info">
        <div class="light-name">${escHtml(l.name)} <span class="tag ${tagClass}">${l.type}</span></div>
        <div class="light-detail">WLED: ${escHtml(l.wledHost || '(not set)')}:${l.wledPort || 80} | Endpoint ${10 + i}</div>
      </div>
      <div>
        <button class="btn btn-sm" style="background:#0f3460" onclick="editLight(${i})">Edit</button>
        <button class="btn btn-danger btn-sm" onclick="deleteLight(${i})">Del</button>
      </div>
    </div>`;
  }).join('');
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function showAddLight() {
  document.getElementById('modalTitle').textContent = 'Add Light';
  document.getElementById('editIndex').value = -1;
  document.getElementById('lightName').value = 'Light ' + (config.lights.length + 1);
  document.getElementById('lightType').value = 'RGB';
  document.getElementById('wledHost').value = '';
  document.getElementById('wledPort').value = 80;
  clearDiscovery();
  document.getElementById('lightModal').classList.add('active');
}

function editLight(i) {
  const l = config.lights[i];
  document.getElementById('modalTitle').textContent = 'Edit Light';
  document.getElementById('editIndex').value = i;
  document.getElementById('lightName').value = l.name;
  document.getElementById('lightType').value = l.type;
  document.getElementById('wledHost').value = l.wledHost || '';
  document.getElementById('wledPort').value = l.wledPort || 80;
  clearDiscovery();
  document.getElementById('lightModal').classList.add('active');
}

function closeModal() {
  document.getElementById('lightModal').classList.remove('active');
}

function clearDiscovery() {
  document.getElementById('discoveryResults').innerHTML =
    '<div class="discover-status" id="discoveryStatus">Click "Scan" to find WLED devices</div>';
}

async function discoverWled() {
  const btn = document.getElementById('discoverBtn');
  const results = document.getElementById('discoveryResults');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  results.innerHTML = '<div class="discover-status"><span class="spinner"></span> Scanning network for WLED devices...</div>';

  try {
    const r = await fetch('/api/wled/discover');
    const data = await r.json();
    const devices = data.devices || [];

    if (devices.length === 0) {
      results.innerHTML = '<div class="discover-status">No WLED devices found. Make sure they are on the same network.</div>';
    } else {
      results.innerHTML = devices.map((d, i) => {
        const preferredHost = d.hostname || d.host;
        const hostDisplay = d.hostname ? d.hostname + ' (' + d.host + ')' : d.host;
        return `<div class="wled-device" onclick="selectWledDevice(this, '${escAttr(preferredHost)}', ${d.port}, '${escAttr(d.name)}', ${d.isRGBW ? 'true' : 'false'})">
          <div class="dev-name">${escHtml(d.name)}</div>
          <div class="dev-detail">${escHtml(hostDisplay)}:${d.port} | ${d.ledCount} LEDs | ${d.isRGBW ? 'RGBW' : 'RGB'} | v${escHtml(d.version)}</div>
        </div>`;
      }).join('');
    }
  } catch(e) {
    results.innerHTML = '<div class="discover-status" style="color:#e74c3c">Discovery failed: ' + escHtml(e.message) + '</div>';
  }

  btn.disabled = false;
  btn.textContent = 'Scan';
}

function escAttr(s) {
  return s.replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

function selectWledDevice(el, host, port, name, isRGBW) {
  // Deselect all
  document.querySelectorAll('.wled-device').forEach(d => d.classList.remove('selected'));
  el.classList.add('selected');

  // Fill in form fields
  document.getElementById('wledHost').value = host;
  document.getElementById('wledPort').value = port;
  document.getElementById('lightType').value = isRGBW ? 'RGBW' : 'RGB';

  // Use WLED device name if the light name is still the default
  const nameField = document.getElementById('lightName');
  if (nameField.value.match(/^Light \d+$/)) {
    nameField.value = name;
  }
}

async function saveLight() {
  const idx = parseInt(document.getElementById('editIndex').value);
  const light = {
    name: document.getElementById('lightName').value || 'Light',
    type: document.getElementById('lightType').value,
    wledHost: document.getElementById('wledHost').value.trim(),
    wledPort: parseInt(document.getElementById('wledPort').value) || 80,
  };

  if (!light.wledHost) {
    alert('Please enter a WLED device IP or hostname, or use discovery to find one.');
    return;
  }

  if (idx >= 0) {
    config.lights[idx] = light;
  } else {
    config.lights.push(light);
  }

  closeModal();
  await postConfig();
  renderLights();
}

async function deleteLight(i) {
  if (!confirm('Delete "' + config.lights[i].name + '"?')) return;
  config.lights.splice(i, 1);
  await postConfig();
  renderLights();
}

async function postConfig() {
  try {
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config)
    });
  } catch(e) {
    alert('Failed to save configuration');
  }
}

async function saveWifi() {
  const ssid = document.getElementById('wifiSSID').value;
  const pass = document.getElementById('wifiPass').value;
  if (!ssid) { alert('Please enter SSID'); return; }

  try {
    await fetch('/api/wifi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ssid, password: pass })
    });
    alert('WiFi credentials saved. The device will now restart and connect to your network.');
  } catch(e) {
    alert('Failed to save WiFi settings');
  }
}

async function factoryReset() {
  if (!confirm('This will erase all configuration including WiFi and light settings. Continue?')) return;
  try {
    await fetch('/api/factory-reset', { method: 'POST' });
    alert('Factory reset complete. Device will restart.');
  } catch(e) {
    alert('Failed to factory reset');
  }
}

async function restartDevice() {
  if (!confirm('Restart the device?')) return;
  try {
    await fetch('/api/restart', { method: 'POST' });
    document.getElementById('wifiStatus').textContent = 'Restarting...';
    document.getElementById('zbStatus').textContent = '...';
  } catch(e) { /* expected - device is restarting */ }
}

function uploadOTA() {
  const fileInput = document.getElementById('otaFile');
  if (!fileInput.files.length) { alert('Please select a firmware file'); return; }
  if (!confirm('Upload and flash firmware? The device will restart after flashing.')) return;

  const file = fileInput.files[0];
  const formData = new FormData();
  formData.append('firmware', file);

  const xhr = new XMLHttpRequest();
  const progress = document.getElementById('otaProgress');
  const bar = document.getElementById('otaBar');
  const status = document.getElementById('otaStatus');

  progress.style.display = 'block';
  status.textContent = 'Uploading...';
  bar.style.width = '0%';

  xhr.upload.addEventListener('progress', function(e) {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      bar.style.width = pct + '%';
      status.textContent = 'Uploading: ' + pct + '%';
    }
  });

  xhr.addEventListener('load', function() {
    if (xhr.status === 200) {
      bar.style.width = '100%';
      status.textContent = 'Flashing complete! Device is restarting...';
      status.style.color = '#00d4ff';
    } else {
      status.textContent = 'Upload failed: ' + xhr.responseText;
      status.style.color = '#e74c3c';
    }
  });

  xhr.addEventListener('error', function() {
    status.textContent = 'Upload failed (network error)';
    status.style.color = '#e74c3c';
  });

  xhr.open('POST', '/api/ota');
  xhr.send(formData);
}

// Poll helpers: wait for request to finish before scheduling next
function pollStatus() {
  loadStatus().finally(() => setTimeout(pollStatus, 5000));
}
function pollLightStates() {
  loadLightStates().finally(() => setTimeout(pollLightStates, 2000));
}

// ---- SSE (Server-Sent Events) with polling fallback ----
let evtSource = null;
let useSSE = true;

function applyStatus(s) {
  document.getElementById('wifiStatus').textContent = s.wifi || 'Unknown';
  document.getElementById('zbStatus').textContent = s.zigbee || 'Unknown';
  isApMode = s.apMode || false;
  document.getElementById('wifiSetup').style.display = isApMode ? 'block' : 'none';
}

function applyLightStates(data) {
  const lights = data.lights || [];
  for (let i = 0; i < lights.length; i++) {
    const el = document.getElementById('preview-' + i);
    if (!el) continue;
    const l = lights[i];
    if (l.on) {
      const pw = l.w || 0;
      const pr = Math.min(255, l.r + pw);
      const pg = Math.min(255, l.g + pw);
      const pb = Math.min(255, l.b + pw);
      el.style.background = 'rgb(' + pr + ',' + pg + ',' + pb + ')';
      el.style.boxShadow = '0 0 8px rgba(' + pr + ',' + pg + ',' + pb + ',0.5)';
    } else {
      el.style.background = '#111';
      el.style.boxShadow = 'none';
    }
  }
}

function startSSE() {
  if (evtSource) { evtSource.close(); evtSource = null; }

  evtSource = new EventSource('/api/events');

  evtSource.addEventListener('status', function(e) {
    try { applyStatus(JSON.parse(e.data)); } catch(err) {}
  });

  evtSource.addEventListener('lightstate', function(e) {
    try { applyLightStates(JSON.parse(e.data)); } catch(err) {}
  });

  evtSource.onerror = function() {
    // EventSource auto-reconnects, but if it keeps failing fall back to polling
    if (evtSource.readyState === EventSource.CLOSED) {
      console.warn('SSE closed, falling back to polling');
      useSSE = false;
      evtSource.close();
      evtSource = null;
      setTimeout(pollStatus, 5000);
      setTimeout(pollLightStates, 2000);
    }
  };
}

// Initial load - fetch config and status, then start SSE
Promise.all([loadStatus(), loadConfig()]).then(() => {
  document.getElementById('loadingState')?.remove();
  if (useSSE) {
    startSSE();
  } else {
    setTimeout(pollStatus, 5000);
    setTimeout(pollLightStates, 2000);
  }
});
</script>
</body>
</html>
)rawliteral";

// ---- Helper: read full POST body from esp_http_server request ----
static String readRequestBody(httpd_req_t *req) {
  int contentLen = req->content_len;
  if (contentLen <= 0) return String();

  // Cap to a reasonable size to avoid OOM
  if (contentLen > 4096) contentLen = 4096;

  String body;
  body.reserve(contentLen);
  char buf[256];
  int remaining = contentLen;

  while (remaining > 0) {
    int toRead = (remaining < (int)sizeof(buf)) ? remaining : (int)sizeof(buf);
    int ret = httpd_req_recv(req, buf, toRead);
    if (ret <= 0) {
      if (ret == HTTPD_SOCK_ERR_TIMEOUT) continue;  // retry on timeout
      break;
    }
    body.concat(buf, ret);
    remaining -= ret;
  }
  return body;
}

// ---- Helper: send JSON string as response ----
static esp_err_t sendJson(httpd_req_t *req, const String& json) {
  httpd_resp_set_type(req, "application/json");
  return httpd_resp_send(req, json.c_str(), json.length());
}

// ---- Helper: send a short JSON error ----
static esp_err_t sendJsonError(httpd_req_t *req, int status, const char* msg) {
  char statusStr[8];
  snprintf(statusStr, sizeof(statusStr), "%d", status);
  httpd_resp_set_status(req, status == 400 ? "400 Bad Request" : "500 Internal Server Error");
  httpd_resp_set_type(req, "application/json");
  String body = "{\"error\":\"";
  body += msg;
  body += "\"}";
  return httpd_resp_send(req, body.c_str(), body.length());
}

// ---- Helper: load WiFi creds from NVS ----
static void loadWifiCreds() {
  Preferences p;
  if (!p.begin("zbwled_wifi", false)) {
    ESP_LOGW("Web", "WiFi NVS namespace init failed");
    return;
  }
  wifiSSID = p.getString("ssid", "");
  wifiPass = p.getString("pass", "");
  p.end();
}

static void saveWifiCreds(const String& ssid, const String& pass) {
  Preferences p;
  p.begin("zbwled_wifi", false);
  p.putString("ssid", ssid);
  p.putString("pass", pass);
  p.end();
}

// ---- Route handlers ----

// GET / — Serve the main page
static esp_err_t handleRoot(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, INDEX_HTML, strlen(INDEX_HTML));
}

// GET /api/status
static esp_err_t handleStatus(httpd_req_t *req) {
  JsonDocument doc;
  doc["wifi"] = WiFi.isConnected() ? WiFi.SSID() : "Not connected";
  doc["apMode"] = apMode;
  doc["zigbee"] = !zigbeeIsEnabled() ? "Disabled - no lights configured"
                 : zigbeeIsPaired() ? "Paired"
                 : "Searching...";
  doc["eui64"] = zigbeeGetEUI64();
  doc["lightCount"] = configStore.getLightCount();

  String json;
  serializeJson(doc, json);
  return sendJson(req, json);
}

// GET /api/lights/state
static esp_err_t handleLightState(httpd_req_t *req) {
  JsonDocument doc;
  JsonArray arr = doc["lights"].to<JsonArray>();
  uint8_t count = configStore.getLightCount();
  for (uint8_t i = 0; i < count; i++) {
    const LightState& st = zigbeeGetLightState(i);
    JsonObject obj = arr.add<JsonObject>();
    obj["on"] = st.powerOn;
    obj["bri"] = st.brightness;
    float briScale = st.powerOn ? (static_cast<float>(st.brightness) / 254.0f) : 0.0f;
    obj["r"] = static_cast<uint8_t>(st.red * briScale);
    obj["g"] = static_cast<uint8_t>(st.green * briScale);
    obj["b"] = static_cast<uint8_t>(st.blue * briScale);
    obj["w"] = static_cast<uint8_t>(st.white * briScale);
    obj["x"] = serialized(String(st.colorX, 4));
    obj["y"] = serialized(String(st.colorY, 4));
  }
  String json;
  serializeJson(doc, json);
  return sendJson(req, json);
}

// GET /api/wled/discover
static esp_err_t handleWledDiscover(httpd_req_t *req) {
  std::vector<WledDeviceInfo> devices;
  wledDiscover(devices);

  JsonDocument doc;
  JsonArray arr = doc["devices"].to<JsonArray>();
  for (const auto& dev : devices) {
    JsonObject obj = arr.add<JsonObject>();
    obj["name"] = dev.name;
    obj["host"] = dev.host;
    obj["hostname"] = dev.hostname;
    obj["port"] = dev.port;
    obj["mac"] = dev.mac;
    obj["ledCount"] = dev.ledCount;
    obj["isRGBW"] = dev.isRGBW;
    obj["version"] = dev.version;
  }

  String json;
  serializeJson(doc, json);
  return sendJson(req, json);
}

// GET /api/config
static esp_err_t handleConfigGet(httpd_req_t *req) {
  JsonDocument doc;
  configStore.toJson(doc);

  String json;
  serializeJson(doc, json);
  return sendJson(req, json);
}

// POST /api/config
static esp_err_t handleConfigPost(httpd_req_t *req) {
  String body = readRequestBody(req);
  if (body.length() == 0) {
    return sendJsonError(req, 400, "No body");
  }

  ESP_LOGI("Web", "POST /api/config body (%d bytes): %s", body.length(), body.c_str());

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);

  if (err) {
    ESP_LOGE("Web", "JSON parse error: %s", err.c_str());
    String resp = "{\"error\":\"Invalid JSON: ";
    resp += err.c_str();
    resp += "\"}";
    httpd_resp_set_status(req, "400 Bad Request");
    return sendJson(req, resp);
  }

  if (configStore.fromJson(doc)) {
    configStore.save();
    ESP_LOGI("Web", "Config saved: %d lights", configStore.getLightCount());
    zigbeeReconfigure();
    return sendJson(req, "{\"ok\":true}");
  } else {
    ESP_LOGE("Web", "fromJson failed - doc contents:");
    String docStr;
    serializeJson(doc, docStr);
    ESP_LOGE("Web", "  parsed doc: %s", docStr.c_str());
    return sendJsonError(req, 400, "Invalid config - 'lights' array not found");
  }
}

// POST /api/wifi
static esp_err_t handleWifiPost(httpd_req_t *req) {
  String body = readRequestBody(req);
  if (body.length() == 0) {
    return sendJsonError(req, 400, "No body");
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);

  if (err) {
    return sendJsonError(req, 400, "Invalid JSON");
  }

  String ssid = doc["ssid"] | "";
  String pass = doc["password"] | "";

  if (ssid.length() == 0) {
    return sendJsonError(req, 400, "SSID required");
  }

  saveWifiCreds(ssid, pass);
  sendJson(req, "{\"ok\":true}");

  // Restart after a short delay to let the response be sent
  delay(1000);
  ESP.restart();
  return ESP_OK;
}

// POST /api/factory-reset
static esp_err_t handleFactoryReset(httpd_req_t *req) {
  configStore.factoryReset();

  // Also clear WiFi credentials
  Preferences p;
  p.begin("zbwled_wifi", false);
  p.clear();
  p.end();

  sendJson(req, "{\"ok\":true}");

  delay(1000);
  ESP.restart();
  return ESP_OK;
}

// POST /api/restart
static esp_err_t handleRestart(httpd_req_t *req) {
  sendJson(req, "{\"ok\":true}");
  delay(1000);
  ESP.restart();
  return ESP_OK;
}

// POST /api/ota — multipart firmware upload
static esp_err_t handleOta(httpd_req_t *req) {
  int contentLen = req->content_len;
  if (contentLen <= 0) {
    httpd_resp_set_status(req, "400 Bad Request");
    return httpd_resp_send(req, "No content", HTTPD_RESP_USE_STRLEN);
  }

  ESP_LOGI("OTA", "OTA upload start (%d bytes)", contentLen);
  wledOutput.stop();

  // Extract multipart boundary from Content-Type header
  char contentType[128] = {0};
  if (httpd_req_get_hdr_value_str(req, "Content-Type", contentType, sizeof(contentType)) != ESP_OK) {
    httpd_resp_set_status(req, "400 Bad Request");
    return httpd_resp_send(req, "Missing Content-Type", HTTPD_RESP_USE_STRLEN);
  }

  char *boundaryPtr = strstr(contentType, "boundary=");
  if (!boundaryPtr) {
    httpd_resp_set_status(req, "400 Bad Request");
    return httpd_resp_send(req, "Missing boundary", HTTPD_RESP_USE_STRLEN);
  }
  boundaryPtr += 9;  // skip "boundary="

  // Build the full boundary markers
  String boundary = String("--") + boundaryPtr;
  String endBoundary = boundary + "--";

  // We'll stream the request body, stripping multipart framing, and feed
  // the raw binary to Update.  Strategy: read all data, skip headers up to
  // the first blank line after the first boundary, then feed until the final
  // boundary.

  // State machine for multipart parsing
  enum ParseState { SEEK_HEADER_END, WRITING, DONE };
  ParseState state = SEEK_HEADER_END;

  if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
    ESP_LOGE("OTA", "Update.begin failed: %s", Update.errorString());
    httpd_resp_set_status(req, "500 Internal Server Error");
    return httpd_resp_send(req, "Update.begin failed", HTTPD_RESP_USE_STRLEN);
  }

  // Accumulator for finding header end and boundary markers
  String accum;
  accum.reserve(512);

  char buf[1024];
  int remaining = contentLen;
  bool updateOk = true;

  while (remaining > 0) {
    int toRead = (remaining < (int)sizeof(buf)) ? remaining : (int)sizeof(buf);
    int ret = httpd_req_recv(req, buf, toRead);
    if (ret <= 0) {
      if (ret == HTTPD_SOCK_ERR_TIMEOUT) continue;
      ESP_LOGE("OTA", "Receive error: %d", ret);
      updateOk = false;
      break;
    }
    remaining -= ret;

    if (state == SEEK_HEADER_END) {
      // Accumulate until we find the blank line after MIME headers (\r\n\r\n)
      accum.concat(buf, ret);
      int headerEnd = accum.indexOf("\r\n\r\n");
      if (headerEnd >= 0) {
        // Everything after \r\n\r\n is firmware data
        int dataStart = headerEnd + 4;
        int dataLen = accum.length() - dataStart;
        if (dataLen > 0) {
          // Check if the end boundary is within this chunk
          int endIdx = accum.indexOf(endBoundary, dataStart);
          if (endIdx >= 0) {
            dataLen = endIdx - dataStart;
            // Strip trailing \r\n before boundary
            if (dataLen >= 2 && accum[dataStart + dataLen - 2] == '\r' && accum[dataStart + dataLen - 1] == '\n') {
              dataLen -= 2;
            }
            if (dataLen > 0) {
              Update.write((uint8_t*)accum.c_str() + dataStart, dataLen);
            }
            state = DONE;
          } else {
            Update.write((uint8_t*)accum.c_str() + dataStart, dataLen);
            state = WRITING;
          }
        } else {
          state = WRITING;
        }
        accum = "";  // free memory
      }
    } else if (state == WRITING) {
      // Check if this chunk contains the end boundary
      // We need to be careful about boundaries spanning chunks, so we buffer
      // enough overlap.  The boundary is relatively short compared to chunks.
      accum.concat(buf, ret);

      int endIdx = accum.indexOf(endBoundary);
      if (endIdx >= 0) {
        // Write everything up to (but not including) the boundary
        int dataLen = endIdx;
        // Strip trailing \r\n before boundary
        if (dataLen >= 2 && accum[dataLen - 2] == '\r' && accum[dataLen - 1] == '\n') {
          dataLen -= 2;
        }
        if (dataLen > 0) {
          if (Update.write((uint8_t*)accum.c_str(), dataLen) != (size_t)dataLen) {
            ESP_LOGE("OTA", "Update.write failed: %s", Update.errorString());
            updateOk = false;
          }
        }
        state = DONE;
        accum = "";
      } else {
        // Write all but the last (boundary.length + 4) bytes to handle
        // boundaries that span chunk edges
        int safe = accum.length() - (boundary.length() + 4);
        if (safe > 0) {
          if (Update.write((uint8_t*)accum.c_str(), safe) != (size_t)safe) {
            ESP_LOGE("OTA", "Update.write failed: %s", Update.errorString());
            updateOk = false;
            break;
          }
          accum.remove(0, safe);
        }
      }
    }
    // state == DONE: just drain remaining bytes
  }

  // If we ended in WRITING state (no end boundary found), flush remaining data
  // minus any trailing boundary content
  if (state == WRITING && accum.length() > 0) {
    int endIdx = accum.indexOf(boundary);
    int dataLen = (endIdx >= 0) ? endIdx : accum.length();
    // Strip trailing \r\n
    if (dataLen >= 2 && accum[dataLen - 2] == '\r' && accum[dataLen - 1] == '\n') {
      dataLen -= 2;
    }
    if (dataLen > 0) {
      if (Update.write((uint8_t*)accum.c_str(), dataLen) != (size_t)dataLen) {
        ESP_LOGE("OTA", "Update.write failed: %s", Update.errorString());
        updateOk = false;
      }
    }
  }

  if (updateOk && Update.end(true)) {
    ESP_LOGI("OTA", "OTA upload complete");
    httpd_resp_send(req, "OK", HTTPD_RESP_USE_STRLEN);
    delay(1000);
    ESP.restart();
  } else {
    ESP_LOGE("OTA", "Update.end failed: %s", Update.errorString());
    httpd_resp_set_status(req, "500 Internal Server Error");
    httpd_resp_send(req, "OTA update failed", HTTPD_RESP_USE_STRLEN);
  }

  return ESP_OK;
}

// ---- SSE (Server-Sent Events) — async push from webLoop() ----
//
// The SSE handler sends HTTP headers + initial state, then stores the
// socket fd and async request handle.  webLoop() periodically checks
// for state changes and pushes events via httpd_socket_send().  This
// avoids blocking an httpd worker thread for the SSE connection lifetime.
//
// Only one SSE client is supported at a time (typical for a config UI).
// If a new client connects, the old one is closed.

// Snapshot of status fields for change detection
struct StatusSnapshot {
  bool     connected;
  bool     apMode;
  bool     zigbeeEnabled;
  bool     zigbeePaired;
  uint8_t  lightCount;
};

// Compact light state snapshot for change detection (avoids full JSON compare)
struct LightSnapshot {
  bool    powerOn;
  uint8_t brightness;
  uint8_t r, g, b, w;
};

// SSE client state
static int         sseFd       = -1;   // socket fd, -1 = no client
static httpd_req_t *sseAsyncReq = nullptr;

// Snapshots for change detection (updated from webLoop)
static StatusSnapshot sseLastStatus = {};
static LightSnapshot  sseLastLights[MAX_LIGHTS] = {};
static uint8_t        sseLastCount = 0;
static unsigned long   sseLastKeepalive = 0;

// Build a status SSE data payload (same fields as GET /api/status)
static String buildStatusJson() {
  JsonDocument doc;
  doc["wifi"] = WiFi.isConnected() ? WiFi.SSID() : "Not connected";
  doc["apMode"] = apMode;
  doc["zigbee"] = !zigbeeIsEnabled() ? "Disabled - no lights configured"
                 : zigbeeIsPaired() ? "Paired"
                 : "Searching...";
  doc["eui64"] = zigbeeGetEUI64();
  doc["lightCount"] = configStore.getLightCount();
  String json;
  serializeJson(doc, json);
  return json;
}

// Build a lightstate SSE data payload (same fields as GET /api/lights/state)
static String buildLightStateJson() {
  JsonDocument doc;
  JsonArray arr = doc["lights"].to<JsonArray>();
  uint8_t count = configStore.getLightCount();
  for (uint8_t i = 0; i < count; i++) {
    const LightState& st = zigbeeGetLightState(i);
    JsonObject obj = arr.add<JsonObject>();
    obj["on"] = st.powerOn;
    obj["bri"] = st.brightness;
    float briScale = st.powerOn ? (static_cast<float>(st.brightness) / 254.0f) : 0.0f;
    obj["r"] = static_cast<uint8_t>(st.red * briScale);
    obj["g"] = static_cast<uint8_t>(st.green * briScale);
    obj["b"] = static_cast<uint8_t>(st.blue * briScale);
    obj["w"] = static_cast<uint8_t>(st.white * briScale);
    obj["x"] = serialized(String(st.colorX, 4));
    obj["y"] = serialized(String(st.colorY, 4));
  }
  String json;
  serializeJson(doc, json);
  return json;
}

// Take a snapshot of current status for change detection
static StatusSnapshot takeStatusSnapshot() {
  StatusSnapshot s;
  s.connected     = WiFi.isConnected();
  s.apMode        = apMode;
  s.zigbeeEnabled = zigbeeIsEnabled();
  s.zigbeePaired  = zigbeeIsPaired();
  s.lightCount    = configStore.getLightCount();
  return s;
}

static bool statusChanged(const StatusSnapshot& a, const StatusSnapshot& b) {
  return a.connected != b.connected
      || a.apMode != b.apMode
      || a.zigbeeEnabled != b.zigbeeEnabled
      || a.zigbeePaired != b.zigbeePaired
      || a.lightCount != b.lightCount;
}

static void takeLightSnapshot(LightSnapshot* out, uint8_t count) {
  for (uint8_t i = 0; i < count; i++) {
    const LightState& st = zigbeeGetLightState(i);
    float briScale = st.powerOn ? (static_cast<float>(st.brightness) / 254.0f) : 0.0f;
    out[i].powerOn    = st.powerOn;
    out[i].brightness = st.brightness;
    out[i].r = static_cast<uint8_t>(st.red * briScale);
    out[i].g = static_cast<uint8_t>(st.green * briScale);
    out[i].b = static_cast<uint8_t>(st.blue * briScale);
    out[i].w = static_cast<uint8_t>(st.white * briScale);
  }
}

static bool lightsChanged(const LightSnapshot* a, const LightSnapshot* b, uint8_t count) {
  return memcmp(a, b, count * sizeof(LightSnapshot)) != 0;
}

// Close the current SSE client and release async request
static void sseCloseClient() {
  if (sseFd >= 0 && httpServer) {
    httpd_sess_trigger_close(httpServer, sseFd);
  }
  if (sseAsyncReq) {
    httpd_req_async_handler_complete(sseAsyncReq);
    sseAsyncReq = nullptr;
  }
  sseFd = -1;
}

// Send raw SSE-formatted data on the stored socket.
// Returns true on success, false on failure (client gone).
static bool sseSendRaw(const char* buf, size_t len) {
  if (sseFd < 0 || !httpServer) return false;
  // httpd_socket_send returns bytes sent, or <0 on error
  int sent = httpd_socket_send(httpServer, sseFd, buf, len, 0);
  return (sent >= 0 && (size_t)sent == len);
}

static bool sseSendEvent(const char* event, const String& data) {
  String msg = "event: ";
  msg += event;
  msg += "\ndata: ";
  msg += data;
  msg += "\n\n";
  return sseSendRaw(msg.c_str(), msg.length());
}

// Global session close callback — registered in httpd_config_t.close_fn.
// Called for EVERY session close, so we check if it's our SSE client.
// IMPORTANT: when close_fn is set, the server does NOT close the socket
// itself — we must call close(sockfd).
static void sseSessionCloseCb(httpd_handle_t hd, int sockfd) {
  if (sockfd == sseFd) {
    ESP_LOGI("Web", "SSE client disconnected (fd %d)", sockfd);
    if (sseAsyncReq) {
      httpd_req_async_handler_complete(sseAsyncReq);
      sseAsyncReq = nullptr;
    }
    sseFd = -1;
  }
  close(sockfd);
}

// GET /api/events — SSE event stream
// Sends HTTP headers + initial events, stores socket for async push, returns.
static esp_err_t handleEvents(httpd_req_t *req) {
  // If there's already an SSE client, close the old one
  if (sseFd >= 0) {
    ESP_LOGW("Web", "Replacing existing SSE client (fd %d)", sseFd);
    sseCloseClient();
  }

  // Set SSE headers and send the initial response line + headers via chunked encoding
  httpd_resp_set_type(req, "text/event-stream");
  httpd_resp_set_hdr(req, "Cache-Control", "no-cache");
  httpd_resp_set_hdr(req, "Connection", "keep-alive");

  // Send initial state as the first chunks
  String statusEvt = "event: status\ndata: " + buildStatusJson() + "\n\n";
  String lightEvt  = "event: lightstate\ndata: " + buildLightStateJson() + "\n\n";
  String initial = statusEvt + lightEvt;

  esp_err_t ret = httpd_resp_send_chunk(req, initial.c_str(), initial.length());
  if (ret != ESP_OK) {
    ESP_LOGW("Web", "SSE client disconnected during initial send");
    httpd_resp_send_chunk(req, nullptr, 0);  // end chunked
    return ESP_OK;
  }

  // Get the socket fd and create an async request copy
  int fd = httpd_req_to_sockfd(req);
  httpd_req_t *asyncReq = nullptr;
  ret = httpd_req_async_handler_begin(req, &asyncReq);
  if (ret != ESP_OK) {
    ESP_LOGE("Web", "Failed to begin async handler: %s", esp_err_to_name(ret));
    httpd_resp_send_chunk(req, nullptr, 0);
    return ESP_OK;
  }

  // Session close detection is handled by the global close_fn callback
  // (sseSessionCloseCb) registered in httpd_config_t.

  // Store the SSE client state
  sseFd       = fd;
  sseAsyncReq = asyncReq;
  sseLastStatus = takeStatusSnapshot();
  sseLastCount = configStore.getLightCount();
  takeLightSnapshot(sseLastLights, sseLastCount);
  sseLastKeepalive = millis();

  ESP_LOGI("Web", "SSE client connected (fd %d)", fd);
  return ESP_OK;
}

// Called from webLoop() to push SSE events when state changes
static void ssePollAndPush() {
  if (sseFd < 0) return;

  bool changed = false;

  // Check status changes
  StatusSnapshot curStatus = takeStatusSnapshot();
  if (statusChanged(sseLastStatus, curStatus)) {
    sseLastStatus = curStatus;
    if (!sseSendEvent("status", buildStatusJson())) {
      ESP_LOGW("Web", "SSE send failed (status), closing");
      sseCloseClient();
      return;
    }
    changed = true;
  }

  // Check light state changes
  uint8_t curCount = configStore.getLightCount();
  LightSnapshot curLights[MAX_LIGHTS] = {};
  takeLightSnapshot(curLights, curCount);

  if (curCount != sseLastCount || lightsChanged(sseLastLights, curLights, curCount)) {
    sseLastCount = curCount;
    memcpy(sseLastLights, curLights, sizeof(sseLastLights));
    if (!sseSendEvent("lightstate", buildLightStateJson())) {
      ESP_LOGW("Web", "SSE send failed (lightstate), closing");
      sseCloseClient();
      return;
    }
    changed = true;
  }

  // Keepalive every ~15 seconds if no events were sent
  if (!changed && millis() - sseLastKeepalive >= 15000) {
    sseLastKeepalive = millis();
    if (!sseSendRaw(": keepalive\n\n", 13)) {
      ESP_LOGW("Web", "SSE keepalive failed, closing");
      sseCloseClient();
      return;
    }
  }

  if (changed) {
    sseLastKeepalive = millis();
  }
}

// Catch-all handler for captive portal redirect / 404
static esp_err_t handleNotFound(httpd_req_t *req) {
  if (apMode) {
    httpd_resp_set_status(req, "302 Found");
    httpd_resp_set_hdr(req, "Location", "http://192.168.4.1/");
    return httpd_resp_send(req, "Redirecting to captive portal", HTTPD_RESP_USE_STRLEN);
  }
  httpd_resp_set_status(req, "404 Not Found");
  return httpd_resp_send(req, "Not found", HTTPD_RESP_USE_STRLEN);
}

// ---- Setup API routes ----
static void setupRoutes() {
  // GET /
  static const httpd_uri_t rootUri = {
    .uri       = "/",
    .method    = HTTP_GET,
    .handler   = handleRoot,
    .user_ctx  = nullptr
  };
  httpd_register_uri_handler(httpServer, &rootUri);

  // GET /api/status
  static const httpd_uri_t statusUri = {
    .uri       = "/api/status",
    .method    = HTTP_GET,
    .handler   = handleStatus,
    .user_ctx  = nullptr
  };
  httpd_register_uri_handler(httpServer, &statusUri);

  // GET /api/lights/state
  static const httpd_uri_t lightStateUri = {
    .uri       = "/api/lights/state",
    .method    = HTTP_GET,
    .handler   = handleLightState,
    .user_ctx  = nullptr
  };
  httpd_register_uri_handler(httpServer, &lightStateUri);

  // GET /api/wled/discover
  static const httpd_uri_t discoverUri = {
    .uri       = "/api/wled/discover",
    .method    = HTTP_GET,
    .handler   = handleWledDiscover,
    .user_ctx  = nullptr
  };
  httpd_register_uri_handler(httpServer, &discoverUri);

  // GET /api/config
  static const httpd_uri_t configGetUri = {
    .uri       = "/api/config",
    .method    = HTTP_GET,
    .handler   = handleConfigGet,
    .user_ctx  = nullptr
  };
  httpd_register_uri_handler(httpServer, &configGetUri);

  // POST /api/config
  static const httpd_uri_t configPostUri = {
    .uri       = "/api/config",
    .method    = HTTP_POST,
    .handler   = handleConfigPost,
    .user_ctx  = nullptr
  };
  httpd_register_uri_handler(httpServer, &configPostUri);

  // POST /api/wifi
  static const httpd_uri_t wifiPostUri = {
    .uri       = "/api/wifi",
    .method    = HTTP_POST,
    .handler   = handleWifiPost,
    .user_ctx  = nullptr
  };
  httpd_register_uri_handler(httpServer, &wifiPostUri);

  // POST /api/factory-reset
  static const httpd_uri_t factoryResetUri = {
    .uri       = "/api/factory-reset",
    .method    = HTTP_POST,
    .handler   = handleFactoryReset,
    .user_ctx  = nullptr
  };
  httpd_register_uri_handler(httpServer, &factoryResetUri);

  // POST /api/restart
  static const httpd_uri_t restartUri = {
    .uri       = "/api/restart",
    .method    = HTTP_POST,
    .handler   = handleRestart,
    .user_ctx  = nullptr
  };
  httpd_register_uri_handler(httpServer, &restartUri);

  // POST /api/ota
  static const httpd_uri_t otaUri = {
    .uri       = "/api/ota",
    .method    = HTTP_POST,
    .handler   = handleOta,
    .user_ctx  = nullptr
  };
  httpd_register_uri_handler(httpServer, &otaUri);

  // GET /api/events — SSE event stream
  static const httpd_uri_t eventsUri = {
    .uri       = "/api/events",
    .method    = HTTP_GET,
    .handler   = handleEvents,
    .user_ctx  = nullptr
  };
  httpd_register_uri_handler(httpServer, &eventsUri);
}

// ---- Public functions ----

void webSetup() {
  loadWifiCreds();

  if (wifiSSID.length() > 0) {
    // Try to connect to stored WiFi
    ESP_LOGI("Web", "Connecting to WiFi: %s", wifiSSID.c_str());
    WiFi.mode(WIFI_AP_STA);
    WiFi.begin(wifiSSID.c_str(), wifiPass.c_str());

    // Also start a temporary AP for fallback access
    WiFi.softAP("ZigbeeWLED-Setup", "");
    apMode = true;

    // Wait for connection (non-blocking, with timeout)
    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
      delay(500);
    }

    if (WiFi.status() == WL_CONNECTED) {
      ESP_LOGI("Web", "WiFi connected: %s", WiFi.localIP().toString().c_str());
      apMode = false;
      WiFi.softAPdisconnect(true);
      webOnWifiConnected();
    } else {
      ESP_LOGW("Web", "WiFi connection failed, staying in AP mode");
      // Keep AP running for reconfiguration
    }
  } else {
    // No WiFi configured - start AP mode only
    ESP_LOGI("Web", "No WiFi configured, starting AP mode");
    WiFi.mode(WIFI_AP);
    WiFi.softAP("ZigbeeWLED-Setup", "");
    apMode = true;
  }

  if (apMode) {
    // Start DNS server for captive portal
    dnsServer.start(53, "*", WiFi.softAPIP());
  }

  // Configure and start the ESP-IDF HTTP server
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.max_uri_handlers = 14;
  config.stack_size = 8192;
  config.uri_match_fn = httpd_uri_match_wildcard;
  config.lru_purge_enable = true;
  // Increase recv timeout slightly for large OTA uploads
  config.recv_wait_timeout = 30;
  // Generous send timeout for SSE keepalive on congested coexistence link
  config.send_wait_timeout = 30;
  // Global session close callback for SSE client disconnect detection.
  // Note: when close_fn is set, we are responsible for calling close(sockfd).
  config.close_fn = sseSessionCloseCb;

  esp_err_t err = httpd_start(&httpServer, &config);
  if (err == ESP_OK) {
    setupRoutes();

    // Register catch-all wildcard handler last (matches everything not matched above)
    static const httpd_uri_t catchAllGet = {
      .uri       = "/*",
      .method    = HTTP_GET,
      .handler   = handleNotFound,
      .user_ctx  = nullptr
    };
    httpd_register_uri_handler(httpServer, &catchAllGet);

    static const httpd_uri_t catchAllPost = {
      .uri       = "/*",
      .method    = HTTP_POST,
      .handler   = handleNotFound,
      .user_ctx  = nullptr
    };
    httpd_register_uri_handler(httpServer, &catchAllPost);

    serverStarted = true;
    ESP_LOGI("Web", "HTTP server started%s", apMode ? " (AP mode with captive portal)" : "");
  } else {
    ESP_LOGE("Web", "Failed to start HTTP server: %s", esp_err_to_name(err));
  }
}

void webLoop() {
  if (apMode) {
    dnsServer.processNextRequest();
  }

  // The ESP-IDF HTTP server runs in its own task, so no handleClient() needed.

  // Push SSE events if state changed
  ssePollAndPush();

  // Check if WiFi reconnected (was in AP+STA mode)
  static bool wasConnected = false;
  if (!wasConnected && WiFi.status() == WL_CONNECTED) {
    wasConnected = true;
    apMode = false;
    WiFi.softAPdisconnect(true);
    ESP_LOGI("Web", "WiFi connected: %s", WiFi.localIP().toString().c_str());
    webOnWifiConnected();
  } else if (wasConnected && WiFi.status() != WL_CONNECTED) {
    wasConnected = false;
  }
}

void webOnWifiConnected() {
  // Start Zigbee task once WiFi is up
  zigbeeStart();
}

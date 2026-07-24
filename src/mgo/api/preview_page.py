"""The browser live-preview page.

A single, dependency-free HTML document (no templating engine, no JavaScript
framework) served by the API. It consumes the existing preview endpoints:
``/camera/preview/status`` for state, ``/camera/preview/start`` and
``/camera/preview/stop`` for control, and ``/camera/preview/stream`` for the
live MJPEG image. The browser only ever reads the stream and polls status; it
never owns the camera.
"""

from __future__ import annotations

_PREVIEW_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MGO Live Preview</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; color: #1b1b1b; }
  h1 { font-size: 1.4rem; }
  #stream-wrap { margin: 1rem 0; min-height: 240px; background: #111;
                 display: flex; align-items: center; justify-content: center;
                 border-radius: 6px; }
  #stream { max-width: 100%; display: none; }
  #placeholder { color: #bbb; font-size: 0.95rem; }
  .controls button { font-size: 1rem; padding: 0.4rem 0.9rem; margin-right: 0.5rem; }
  .meta { margin-top: 1rem; border-collapse: collapse; }
  .meta th, .meta td { text-align: left; padding: 0.2rem 1rem 0.2rem 0; }
  .state { font-weight: bold; }
  .error { color: #b00020; }
</style>
</head>
<body>
<h1>Live Preview</h1>

<div class="controls">
  <button id="start-btn" type="button">Start Preview</button>
  <button id="stop-btn" type="button">Stop Preview</button>
  <button id="refresh-btn" type="button">Refresh status</button>
</div>

<div id="stream-wrap">
  <img id="stream" alt="Live preview">
  <span id="placeholder">Preview is not running.</span>
</div>

<table class="meta">
  <tr><th>State</th><td class="state" id="state">unknown</td></tr>
  <tr><th>Resolution</th><td id="resolution">-</td></tr>
  <tr><th>FPS</th><td id="fps">-</td></tr>
  <tr><th>Camera owner</th><td id="owner">-</td></tr>
  <tr><th>Last error</th><td class="error" id="last-error">-</td></tr>
</table>

<script>
(function () {
  "use strict";
  var STATUS_URL = "/camera/preview/status";
  var START_URL = "/camera/preview/start";
  var STOP_URL = "/camera/preview/stop";
  var STREAM_URL = "/camera/preview/stream";
  var POLL_MS = 2000;

  var img = document.getElementById("stream");
  var placeholder = document.getElementById("placeholder");
  var startBtn = document.getElementById("start-btn");
  var stopBtn = document.getElementById("stop-btn");
  var refreshBtn = document.getElementById("refresh-btn");
  var streaming = false;

  function showStream() {
    if (!streaming) {
      // Cache-busting query so a restarted stream reconnects.
      img.src = STREAM_URL + "?t=" + Date.now();
      img.style.display = "block";
      placeholder.style.display = "none";
      streaming = true;
    }
  }

  function hideStream(message) {
    img.style.display = "none";
    img.src = "";
    placeholder.textContent = message;
    placeholder.style.display = "block";
    streaming = false;
  }

  img.addEventListener("error", function () {
    if (streaming) { hideStream("Preview stream ended."); }
  });

  function render(status) {
    document.getElementById("state").textContent = status.state;
    document.getElementById("resolution").textContent = status.resolution;
    document.getElementById("fps").textContent = status.fps;
    document.getElementById("owner").textContent = status.owner || "none";
    document.getElementById("last-error").textContent = status.last_error || "-";

    var running = status.state === "running";
    startBtn.disabled = running || !status.enabled;
    stopBtn.disabled = !running;

    if (running) { showStream(); }
    else { hideStream("Preview is not running."); }
  }

  function refresh() {
    return fetch(STATUS_URL).then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {
        document.getElementById("state").textContent = "unreachable";
      });
  }

  function post(url) {
    startBtn.disabled = true;
    stopBtn.disabled = true;
    return fetch(url, { method: "POST" }).then(refresh).catch(refresh);
  }

  startBtn.addEventListener("click", function () { post(START_URL); });
  stopBtn.addEventListener("click", function () { post(STOP_URL); });
  refreshBtn.addEventListener("click", refresh);

  refresh();
  setInterval(refresh, POLL_MS);
})();
</script>
</body>
</html>
"""


def render_preview_page() -> str:
    """Return the standalone HTML for the live-preview page."""
    return _PREVIEW_PAGE

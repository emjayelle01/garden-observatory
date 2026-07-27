"""The local operational dashboard page.

A single, dependency-free HTML document (no templating engine, no JavaScript
framework, no external asset) served by the API, following the pattern
established by :mod:`mgo.api.preview_page`.

The page is a **static shell**. It renders no server-side value: every live
reading is fetched by the browser from the existing API contracts --
``/health``, ``/version``, ``/motion/status`` and ``/notifications/status`` --
so serving the page collects no health, touches no hardware, opens no database
connection and starts nothing. The browser performs GET requests only.
"""

from __future__ import annotations

_DASHBOARD_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Matt's Garden Observatory &mdash; Dashboard</title>
<style>
  :root {
    --bg: #f6f7f8; --fg: #1b1b1b; --card: #ffffff; --line: #d8dbe0;
    --muted: #4d5158; --ok: #1a7f37; --warn: #7a5600; --crit: #b00020;
    --neutral: #444a51; --active: #0b5cad;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #15181b; --fg: #e8eaed; --card: #1e2226; --line: #343a40;
      --muted: #aab0b7; --ok: #6ac97a; --warn: #e3ad46; --crit: #ff7b8e;
      --neutral: #aab0b7; --active: #6bb7ff;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    margin: 0; padding: 1.25rem; background: var(--bg); color: var(--fg);
    line-height: 1.45;
  }
  header { max-width: 78rem; margin: 0 auto 1rem; }
  main { max-width: 78rem; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin: 0 0 0.25rem; }
  h2 { font-size: 1rem; margin: 0 0 0.5rem; }
  .lede { color: var(--muted); margin: 0 0 0.75rem; font-size: 0.95rem; }
  .bar {
    background: var(--card); border: 1px solid var(--line); border-radius: 8px;
    padding: 0.6rem 0.9rem; display: flex; flex-wrap: wrap; gap: 0.5rem 1.25rem;
    align-items: center; font-size: 0.9rem;
  }
  .bar dl { grid-template-columns: auto auto; gap: 0.15rem 0.5rem; }
  .grid {
    display: grid; gap: 1rem; margin: 1rem 0;
    grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
  }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 8px;
    padding: 0.9rem 1rem;
  }
  .card.wide { grid-column: 1 / -1; }
  dl {
    display: grid; grid-template-columns: auto 1fr; gap: 0.25rem 0.75rem;
    margin: 0; font-size: 0.95rem;
  }
  dt { color: var(--muted); }
  dd { margin: 0; overflow-wrap: anywhere; }
  .badge { font-size: 0.8rem; color: var(--muted); margin: 0 0 0.6rem; }
  .badge.stale { color: var(--crit); font-weight: 600; }
  .pill {
    display: inline-block; font-weight: 600; font-size: 0.9rem;
    padding: 0.05rem 0.55rem; border-radius: 999px;
    border: 1px solid currentColor;
  }
  .pill.big { font-size: 1.1rem; padding: 0.2rem 0.8rem; }
  .s-healthy { color: var(--ok); }
  .s-warning { color: var(--warn); }
  .s-critical { color: var(--crit); }
  .s-active { color: var(--active); }
  .s-neutral { color: var(--neutral); }
  .note { color: var(--muted); font-size: 0.85rem; margin: 0.6rem 0 0; }
  .notice {
    background: var(--card); border: 1px solid var(--line);
    border-left: 4px solid var(--warn); border-radius: 8px;
    padding: 0.75rem 1rem; margin: 0 0 1rem;
  }
  button {
    font: inherit; padding: 0.3rem 0.8rem; border-radius: 6px;
    border: 1px solid var(--line); background: var(--bg); color: var(--fg);
    cursor: pointer;
  }
  a { color: var(--active); }
  a:focus-visible, button:focus-visible {
    outline: 3px solid var(--active); outline-offset: 2px;
  }
  footer { max-width: 78rem; margin: 1.5rem auto 0; color: var(--muted);
           font-size: 0.85rem; }
</style>
</head>
<body>

<header>
  <h1>Matt's Garden Observatory</h1>
  <p class="lede">Local operational dashboard for this appliance. Every
  reading below is reported by the running service; nothing is assumed.</p>

  <noscript>
    <p class="notice"><strong>JavaScript is disabled.</strong> The card
    headings below describe what this dashboard reports, but no live value can
    be loaded and <strong>no status is shown or implied</strong>. This page
    reads <code>/health</code>, <code>/version</code>,
    <code>/motion/status</code> and <code>/notifications/status</code> from
    this service and updates them in the browser; those endpoints can also be
    read directly as JSON. The <a href="/preview">live preview page</a>
    remains available.</p>
  </noscript>

  <div class="bar">
    <dl>
      <dt>Last refresh attempt</dt>
      <dd id="refresh-attempt">Not yet loaded</dd>
      <dt>Last complete refresh</dt>
      <dd id="refresh-complete">Not yet loaded</dd>
      <dt>Latest result</dt>
      <dd id="refresh-outcome">Awaiting first refresh</dd>
    </dl>
    <button id="refresh-now" type="button">Refresh now</button>
  </div>
</header>

<main>
<div class="grid">

  <section class="card wide" data-source="health" aria-labelledby="h-overall">
    <h2 id="h-overall">Overall health</h2>
    <p class="badge">Awaiting live data</p>
    <p><span class="pill big s-neutral" id="overall-status">Not yet
    loaded</span></p>
    <dl>
      <dt>Reported by</dt><dd id="overall-application">Loading&hellip;</dd>
    </dl>
    <p class="note">The worst of the system-resource statuses and the database
    severity. Camera and preview are reported for visibility and never change
    this value.</p>
  </section>

  <section class="card" data-source="version" aria-labelledby="h-identity">
    <h2 id="h-identity">Application identity</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Application</dt><dd id="id-application">Loading&hellip;</dd>
      <dt>Release version</dt><dd id="id-version">Loading&hellip;</dd>
      <dt>Build commit</dt><dd id="id-commit">Loading&hellip;</dd>
      <dt>Python</dt><dd id="id-python">Loading&hellip;</dd>
      <dt>Architecture</dt><dd id="id-architecture">Loading&hellip;</dd>
    </dl>
  </section>

  <section class="card" data-source="health" aria-labelledby="h-hostname">
    <h2 id="h-hostname">Hostname</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Reported hostname</dt><dd id="sys-hostname">Loading&hellip;</dd>
    </dl>
    <p class="note">Exactly as the service reports it &mdash; it identifies
    which machine answered.</p>
  </section>

  <section class="card" data-source="health" aria-labelledby="h-uptime">
    <h2 id="h-uptime">System uptime</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Up for</dt><dd id="sys-uptime">Loading&hellip;</dd>
    </dl>
    <p class="note">Time since the machine booted, not since the application
    or the preview started.</p>
  </section>

  <section class="card" data-source="health" aria-labelledby="h-cpu">
    <h2 id="h-cpu">CPU utilisation</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Current</dt><dd id="cpu-percent">Loading&hellip;</dd>
    </dl>
  </section>

  <section class="card" data-source="health" aria-labelledby="h-temp">
    <h2 id="h-temp">CPU temperature</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Reading</dt><dd id="temp-celsius">Loading&hellip;</dd>
      <dt>Status</dt>
      <dd><span class="pill s-neutral" id="temp-status">Not yet
      loaded</span></dd>
    </dl>
    <p class="note">Raspberry Pi thermal reporting. Where that tooling is
    absent the reading is truthfully unavailable rather than zero.</p>
  </section>

  <section class="card" data-source="health" aria-labelledby="h-memory">
    <h2 id="h-memory">Memory</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Used</dt><dd id="mem-percent">Loading&hellip;</dd>
      <dt>Available</dt><dd id="mem-available">Loading&hellip;</dd>
      <dt>Total</dt><dd id="mem-total">Loading&hellip;</dd>
      <dt>Used bytes</dt><dd id="mem-used">Loading&hellip;</dd>
      <dt>Status</dt>
      <dd><span class="pill s-neutral" id="mem-status">Not yet
      loaded</span></dd>
    </dl>
    <p class="note">Sizes use binary units (KiB, MiB, GiB, TiB). Used bytes
    are derived for display only; the reported total, available and percentage
    are authoritative.</p>
  </section>

  <section class="card" data-source="health" aria-labelledby="h-disk">
    <h2 id="h-disk">Disk</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Used</dt><dd id="disk-percent">Loading&hellip;</dd>
      <dt>Free</dt><dd id="disk-free">Loading&hellip;</dd>
      <dt>Total</dt><dd id="disk-total">Loading&hellip;</dd>
      <dt>Used bytes</dt><dd id="disk-used">Loading&hellip;</dd>
      <dt>Status</dt>
      <dd><span class="pill s-neutral" id="disk-status">Not yet
      loaded</span></dd>
    </dl>
  </section>

  <section class="card" data-source="health" aria-labelledby="h-database">
    <h2 id="h-database">Database</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Status</dt>
      <dd><span class="pill s-neutral" id="db-status">Not yet
      loaded</span></dd>
      <dt>Reachable</dt><dd id="db-accessible">Loading&hellip;</dd>
      <dt>Schema version</dt><dd id="db-schema">Loading&hellip;</dd>
      <dt>Expected schema</dt><dd id="db-expected">Loading&hellip;</dd>
      <dt>Migrations</dt><dd id="db-migration">Loading&hellip;</dd>
      <dt>Integrity</dt><dd id="db-integrity">Loading&hellip;</dd>
    </dl>
    <p class="note">Read from the background monitor's cached result. A
    degraded database is usable but behind or misconfigured; that is not the
    same as an unreachable one.</p>
  </section>

  <section class="card" data-source="health" aria-labelledby="h-camera">
    <h2 id="h-camera">Camera</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Enabled</dt><dd id="cam-enabled">Loading&hellip;</dd>
      <dt>Readiness</dt>
      <dd><span class="pill s-neutral" id="cam-status">Not yet
      loaded</span></dd>
      <dt>Available</dt><dd id="cam-available">Loading&hellip;</dd>
      <dt>Backend</dt><dd id="cam-backend">Loading&hellip;</dd>
      <dt>Detail</dt><dd id="cam-detail">Loading&hellip;</dd>
      <dt>Last checked</dt><dd id="cam-checked">Loading&hellip;</dd>
    </dl>
    <p class="note">Placeholder card. No image is shown here and opening this
    dashboard starts no camera process. Use the
    <a href="/preview">live preview page</a> to view the camera.
    <strong>Bird recognition is not yet implemented.</strong></p>
  </section>

  <section class="card" data-source="health" aria-labelledby="h-preview">
    <h2 id="h-preview">Live preview</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Enabled</dt><dd id="prev-enabled">Loading&hellip;</dd>
      <dt>State</dt>
      <dd><span class="pill s-neutral" id="prev-state">Not yet
      loaded</span></dd>
      <dt>Camera owner</dt><dd id="prev-owner">Loading&hellip;</dd>
      <dt>Running for</dt><dd id="prev-uptime">Loading&hellip;</dd>
    </dl>
    <p class="note">Reported only. Preview is started and stopped on the
    <a href="/preview">live preview page</a>; this dashboard never controls it.
    A stopped or disabled preview is not a fault.</p>
  </section>

  <section class="card" data-source="motion" aria-labelledby="h-motion">
    <h2 id="h-motion">Motion detection</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Enabled</dt><dd id="motion-enabled">Loading&hellip;</dd>
      <dt>Status</dt>
      <dd><span class="pill s-neutral" id="motion-status">Not yet
      loaded</span></dd>
      <dt>Change detected</dt><dd id="motion-detected">Loading&hellip;</dd>
      <dt>Frames available</dt><dd id="motion-frames">Loading&hellip;</dd>
      <dt>Score</dt><dd id="motion-score">Loading&hellip;</dd>
      <dt>Threshold</dt><dd id="motion-threshold">Loading&hellip;</dd>
      <dt>Evaluated</dt><dd id="motion-evaluated">Loading&hellip;</dd>
      <dt>Detail</dt><dd id="motion-detail">Loading&hellip;</dd>
    </dl>
    <p class="note">This measures <strong>scene change</strong> between
    consecutive preview frames. It is not object recognition, not species
    identification and not confirmed wildlife activity.</p>
  </section>

  <section class="card" data-source="notifications"
           aria-labelledby="h-notifications">
    <h2 id="h-notifications">Notifications</h2>
    <p class="badge">Awaiting live data</p>
    <dl>
      <dt>Enabled</dt><dd id="notif-enabled">Loading&hellip;</dd>
      <dt>Providers</dt><dd id="notif-providers">Loading&hellip;</dd>
      <dt>Events published</dt><dd id="notif-published">Loading&hellip;</dd>
      <dt>Delivery failures</dt><dd id="notif-failures">Loading&hellip;</dd>
      <dt>Last event</dt><dd id="notif-last">Loading&hellip;</dd>
    </dl>
    <p class="note">Disabled notifications, and having no provider
    configured, are normal states &mdash; neither is a delivery failure.</p>
  </section>

</div>
</main>

<footer>
  <p>Local appliance dashboard. Read-only: it issues GET requests to this
  service only and changes nothing.</p>
</footer>

<script>
(function () {
  "use strict";

  var HEALTH_URL = "/health";
  var VERSION_URL = "/version";
  var MOTION_URL = "/motion/status";
  var NOTIFICATIONS_URL = "/notifications/status";

  // Completion-scheduled: the next refresh is only queued once the previous
  // cycle has fully settled, so cycles can never overlap or stack up.
  var REFRESH_MS = 10000;

  var UNAVAILABLE = "Unavailable";
  var NOT_REPORTED = "Not reported";
  var NEUTRAL_CLASS = "s-neutral";
  var BYTE_UNITS = ["bytes", "KiB", "MiB", "GiB", "TiB", "PiB"];

  // Known status values map to a fixed internal class; anything unexpected
  // stays visible as text and receives the neutral class. No API-derived
  // value is ever inserted into a class name, attribute, selector or URL.
  var STATUS_CLASSES = {
    "healthy": "s-healthy",
    "warning": "s-warning",
    "critical": "s-critical",
    "unknown": "s-neutral",
    "degraded": "s-warning",
    "unhealthy": "s-critical",
    "disabled": "s-neutral",
    "available": "s-healthy",
    "waiting_for_hardware": "s-warning",
    "error": "s-critical",
    "stopped": "s-neutral",
    "starting": "s-neutral",
    "running": "s-healthy",
    "stopping": "s-neutral",
    "failed": "s-critical",
    "waiting_for_frames": "s-neutral",
    "establishing_baseline": "s-neutral",
    "no_motion": "s-healthy",
    "motion_detected": "s-active"
  };

  // A textual glyph accompanies every status so state is never conveyed by
  // colour alone.
  var STATUS_GLYPHS = {
    "s-healthy": "\\u2713",
    "s-warning": "\\u26A0",
    "s-critical": "\\u2715",
    "s-active": "\\u25CF",
    "s-neutral": "\\u2013"
  };

  var SOURCE_KEYS = ["health", "version", "motion", "notifications"];
  var SOURCE_URLS = {
    "health": HEALTH_URL,
    "version": VERSION_URL,
    "motion": MOTION_URL,
    "notifications": NOTIFICATIONS_URL
  };

  // "never" until an endpoint has answered once; "stale" once a later
  // refresh fails. A failure never clears the last good reading.
  var sourceState = {
    "health": "never",
    "version": "never",
    "motion": "never",
    "notifications": "never"
  };

  var badges = {
    "health": collect('[data-source="health"] .badge'),
    "version": collect('[data-source="version"] .badge'),
    "motion": collect('[data-source="motion"] .badge'),
    "notifications": collect('[data-source="notifications"] .badge')
  };

  var refreshing = false;
  var timer = null;
  var lastAttempt = null;
  var lastComplete = null;
  var lastOutcome = "Awaiting first refresh";

  function collect(selector) {
    var found = document.querySelectorAll(selector);
    var nodes = [];
    var index = 0;
    for (index = 0; index < found.length; index += 1) {
      nodes.push(found[index]);
    }
    return nodes;
  }

  // --- formatting -----------------------------------------------------
  // Every helper checks explicitly for null, undefined and finite numbers.
  // Truthiness is deliberately never used, so a valid 0 is a reading and not
  // treated as a missing value.

  function isNumber(value) {
    return typeof value === "number" && isFinite(value);
  }

  function pick(payload, name) {
    if (payload === null || typeof payload !== "object") {
      return undefined;
    }
    return payload[name];
  }

  function section(payload, name) {
    var value = pick(payload, name);
    if (value === null || typeof value !== "object") {
      return {};
    }
    return value;
  }

  function formatText(value) {
    if (typeof value !== "string") {
      return UNAVAILABLE;
    }
    var trimmed = value.trim();
    if (trimmed.length === 0) {
      return UNAVAILABLE;
    }
    return trimmed;
  }

  function formatFlag(value, yes, no) {
    if (value === true) {
      return yes;
    }
    if (value === false) {
      return no;
    }
    return UNAVAILABLE;
  }

  function formatCount(value) {
    if (!isNumber(value)) {
      return UNAVAILABLE;
    }
    return String(Math.round(value));
  }

  function formatPercent(value) {
    if (!isNumber(value)) {
      return UNAVAILABLE;
    }
    return value.toFixed(1) + " %";
  }

  function formatRatio(value) {
    if (!isNumber(value)) {
      return UNAVAILABLE;
    }
    return value.toFixed(4);
  }

  function formatCelsius(value) {
    if (!isNumber(value)) {
      return NOT_REPORTED;
    }
    return value.toFixed(1) + " \\u00B0C";
  }

  function formatBytes(value) {
    if (!isNumber(value) || value < 0) {
      return UNAVAILABLE;
    }
    var size = value;
    var unit = 0;
    while (size >= 1024 && unit < BYTE_UNITS.length - 1) {
      size = size / 1024;
      unit += 1;
    }
    if (unit === 0) {
      return String(Math.round(size)) + " " + BYTE_UNITS[0];
    }
    return size.toFixed(1) + " " + BYTE_UNITS[unit];
  }

  function plural(count, noun) {
    if (count === 1) {
      return "1 " + noun;
    }
    return String(count) + " " + noun + "s";
  }

  function formatDuration(value) {
    if (!isNumber(value) || value < 0) {
      return UNAVAILABLE;
    }
    var seconds = Math.floor(value);
    if (seconds < 60) {
      return plural(seconds, "second");
    }
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) {
      return plural(minutes, "minute");
    }
    var hours = Math.floor(minutes / 60);
    var restMinutes = minutes % 60;
    if (hours < 24) {
      if (restMinutes === 0) {
        return plural(hours, "hour");
      }
      return plural(hours, "hour") + " " + plural(restMinutes, "minute");
    }
    var days = Math.floor(hours / 24);
    var restHours = hours % 24;
    var parts = [plural(days, "day")];
    if (restHours > 0) {
      parts.push(plural(restHours, "hour"));
    }
    if (restMinutes > 0) {
      parts.push(plural(restMinutes, "minute"));
    }
    return parts.join(" ");
  }

  function formatTimestamp(value) {
    if (typeof value !== "string" || value.trim().length === 0) {
      return UNAVAILABLE;
    }
    var parsed = new Date(value);
    var millis = parsed.getTime();
    if (!isNumber(millis)) {
      return UNAVAILABLE;
    }
    return parsed.toLocaleString();
  }

  function formatProviders(value) {
    if (Object.prototype.toString.call(value) !== "[object Array]") {
      return UNAVAILABLE;
    }
    var names = [];
    var index = 0;
    for (index = 0; index < value.length; index += 1) {
      var name = value[index];
      if (typeof name === "string" && name.trim().length > 0) {
        names.push(name.trim());
      }
    }
    if (names.length === 0) {
      return "None configured";
    }
    return names.join(", ");
  }

  function derivedUsedBytes(total, remaining) {
    if (!isNumber(total) || !isNumber(remaining)) {
      return UNAVAILABLE;
    }
    var used = total - remaining;
    if (used < 0) {
      return UNAVAILABLE;
    }
    return formatBytes(used);
  }

  // --- safe DOM writes ------------------------------------------------
  // API-derived text only ever reaches the DOM through textContent.

  function setText(id, text) {
    var node = document.getElementById(id);
    if (node !== null) {
      node.textContent = text;
    }
  }

  function setStatus(id, value) {
    var node = document.getElementById(id);
    if (node === null) {
      return;
    }
    var known = typeof value === "string" &&
      Object.prototype.hasOwnProperty.call(STATUS_CLASSES, value);
    var className = NEUTRAL_CLASS;
    if (known) {
      className = STATUS_CLASSES[value];
    }
    var glyph = STATUS_GLYPHS[className];
    var big = "";
    if (id === "overall-status") {
      big = " big";
    }
    node.className = "pill" + big + " " + className;
    node.textContent = glyph + " " + formatText(value);
  }

  // --- card renderers -------------------------------------------------

  function renderHealth(payload) {
    setStatus("overall-status", pick(payload, "status"));
    setText("overall-application", formatText(pick(payload, "application")));
    setText("sys-hostname", formatText(pick(payload, "hostname")));
    setText("sys-uptime", formatDuration(pick(payload, "uptime_seconds")));
    setText("cpu-percent", formatPercent(pick(payload, "cpu_percent")));

    var temperature = section(payload, "temperature");
    setText("temp-celsius", formatCelsius(temperature.celsius));
    setStatus("temp-status", temperature.status);

    var memory = section(payload, "memory");
    setText("mem-percent", formatPercent(memory.used_percent));
    setText("mem-available", formatBytes(memory.available_bytes));
    setText("mem-total", formatBytes(memory.total_bytes));
    setText("mem-used", derivedUsedBytes(memory.total_bytes,
      memory.available_bytes));
    setStatus("mem-status", memory.status);

    var disk = section(payload, "disk");
    setText("disk-percent", formatPercent(disk.used_percent));
    setText("disk-free", formatBytes(disk.free_bytes));
    setText("disk-total", formatBytes(disk.total_bytes));
    setText("disk-used", derivedUsedBytes(disk.total_bytes, disk.free_bytes));
    setStatus("disk-status", disk.status);

    var database = section(payload, "database");
    setStatus("db-status", database.status);
    setText("db-accessible", formatFlag(database.accessible, "Reachable",
      "Unreachable"));
    setText("db-schema", formatCount(database.schema_version));
    setText("db-expected", formatCount(database.expected_schema_version));
    setText("db-migration", formatText(database.migration_status));
    setText("db-integrity", formatText(database.integrity));

    var camera = section(payload, "camera");
    setText("cam-enabled", formatFlag(camera.enabled, "Enabled", "Disabled"));
    setStatus("cam-status", camera.status);
    setText("cam-available", formatFlag(camera.available, "Available",
      "Not available"));
    setText("cam-backend", formatText(camera.backend));
    setText("cam-detail", formatText(camera.detail));
    setText("cam-checked", formatTimestamp(camera.checked_at));

    var preview = section(payload, "preview");
    setText("prev-enabled", formatFlag(preview.enabled, "Enabled",
      "Disabled"));
    setStatus("prev-state", preview.state);
    setText("prev-owner", ownerText(preview.owner));
    setText("prev-uptime", previewUptime(preview));
  }

  function ownerText(value) {
    if (value === null || value === undefined) {
      return "No owner";
    }
    return formatText(value);
  }

  function previewUptime(preview) {
    if (preview.state !== "running") {
      return "Not running";
    }
    return formatDuration(preview.uptime_seconds);
  }

  function renderVersion(payload) {
    setText("id-application", formatText(pick(payload, "application")));
    setText("id-version", formatText(pick(payload, "version")));
    setText("id-commit", commitText(pick(payload, "commit")));
    setText("id-python", formatText(pick(payload, "python_version")));
    setText("id-architecture", formatText(pick(payload, "architecture")));
  }

  function commitText(value) {
    if (value === null || value === undefined) {
      return "Not supplied";
    }
    return formatText(value);
  }

  function renderMotion(payload) {
    setText("motion-enabled", formatFlag(pick(payload, "enabled"), "Enabled",
      "Disabled"));
    setStatus("motion-status", pick(payload, "status"));
    setText("motion-detected", formatFlag(pick(payload, "detected"),
      "Scene change detected", "No scene change"));
    var frames = pick(payload, "frames_available");
    setText("motion-frames", formatFlag(frames, "Yes", "No"));
    if (frames === true) {
      setText("motion-score", formatRatio(pick(payload, "score")));
    } else {
      setText("motion-score", "Not measured");
    }
    setText("motion-threshold", formatRatio(pick(payload, "threshold")));
    setText("motion-evaluated", formatTimestamp(pick(payload,
      "evaluated_at")));
    setText("motion-detail", formatText(pick(payload, "detail")));
  }

  function renderNotifications(payload) {
    setText("notif-enabled", formatFlag(pick(payload, "enabled"), "Enabled",
      "Disabled"));
    setText("notif-providers", formatProviders(pick(payload, "providers")));
    setText("notif-published", formatCount(pick(payload,
      "total_events_published")));
    setText("notif-failures", formatCount(pick(payload,
      "total_delivery_failures")));
    setText("notif-last", lastEventText(pick(payload, "last_event_at")));
  }

  function lastEventText(value) {
    if (value === null || value === undefined) {
      return "No event yet";
    }
    return formatTimestamp(value);
  }

  var RENDERERS = {
    "health": renderHealth,
    "version": renderVersion,
    "motion": renderMotion,
    "notifications": renderNotifications
  };

  // --- refresh state --------------------------------------------------

  function renderBadges() {
    var index = 0;
    for (index = 0; index < SOURCE_KEYS.length; index += 1) {
      var key = SOURCE_KEYS[index];
      var state = sourceState[key];
      var text = "";
      var stale = false;
      if (state === "never") {
        text = "Awaiting live data";
      } else if (state === "stale") {
        text = "Stale \\u2014 latest refresh failed; showing the last " +
          "successful reading";
        stale = true;
      } else {
        text = "Live";
      }
      var nodes = badges[key];
      var node = 0;
      for (node = 0; node < nodes.length; node += 1) {
        nodes[node].textContent = text;
        if (stale) {
          nodes[node].className = "badge stale";
        } else {
          nodes[node].className = "badge";
        }
      }
    }
  }

  function renderSummary() {
    if (lastAttempt === null) {
      setText("refresh-attempt", "Not yet loaded");
    } else {
      setText("refresh-attempt", lastAttempt.toLocaleTimeString());
    }
    if (lastComplete === null) {
      setText("refresh-complete", "Never");
    } else {
      setText("refresh-complete", lastComplete.toLocaleTimeString());
    }
    setText("refresh-outcome", lastOutcome);
  }

  function markAllStale() {
    var index = 0;
    for (index = 0; index < SOURCE_KEYS.length; index += 1) {
      sourceState[SOURCE_KEYS[index]] = "stale";
    }
  }

  function fetchJson(url) {
    return fetch(url, {
      method: "GET",
      headers: { "Accept": "application/json" },
      cache: "no-store"
    }).then(function (response) {
      if (response.ok !== true) {
        throw new Error("HTTP " + response.status);
      }
      return response.json();
    });
  }

  function apply(key, payload) {
    try {
      RENDERERS[key](payload);
      return true;
    } catch (error) {
      // A malformed payload degrades that one source visibly; it never
      // clears the card and never stops the other three rendering.
      return false;
    }
  }

  function process(results) {
    var failures = 0;
    var index = 0;
    for (index = 0; index < SOURCE_KEYS.length; index += 1) {
      var key = SOURCE_KEYS[index];
      var result = results[index];
      var ok = false;
      if (result.status === "fulfilled") {
        ok = apply(key, result.value);
      }
      if (ok) {
        sourceState[key] = "loaded";
      } else {
        sourceState[key] = "stale";
        failures += 1;
      }
    }
    lastAttempt = new Date();
    if (failures === 0) {
      lastComplete = lastAttempt;
      lastOutcome = "Complete \\u2014 all four sources answered";
    } else if (failures === SOURCE_KEYS.length) {
      lastOutcome = "Failed \\u2014 no source answered; readings are stale";
    } else {
      lastOutcome = "Partial \\u2014 " + String(failures) + " of " +
        String(SOURCE_KEYS.length) + " sources failed";
    }
    renderBadges();
    renderSummary();
  }

  function schedule() {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
    if (document.hidden === true) {
      return;
    }
    timer = window.setTimeout(refresh, REFRESH_MS);
  }

  function refresh() {
    if (refreshing) {
      return;
    }
    refreshing = true;
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
    var requests = [];
    var index = 0;
    for (index = 0; index < SOURCE_KEYS.length; index += 1) {
      requests.push(fetchJson(SOURCE_URLS[SOURCE_KEYS[index]]));
    }
    Promise.allSettled(requests).then(function (results) {
      process(results);
    }).catch(function () {
      lastAttempt = new Date();
      lastOutcome = "Failed \\u2014 the refresh could not complete";
      markAllStale();
      renderBadges();
      renderSummary();
    }).then(function () {
      refreshing = false;
      schedule();
    });
  }

  document.getElementById("refresh-now").addEventListener("click", refresh);

  document.addEventListener("visibilitychange", function () {
    if (document.hidden === true) {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    } else {
      refresh();
    }
  });

  renderBadges();
  renderSummary();
  refresh();
})();
</script>
</body>
</html>
"""


def render_dashboard_page() -> str:
    """Return the standalone HTML for the local operational dashboard.

    A constant, side-effect-free document: it reads no configuration, collects
    no health, touches no hardware and opens no database connection. Every
    value the page displays is fetched by the browser from the existing API
    contracts.
    """
    return _DASHBOARD_PAGE

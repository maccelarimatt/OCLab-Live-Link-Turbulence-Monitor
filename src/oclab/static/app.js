"use strict";
const $ = (id) => document.getElementById(id);
const fmt = (n, digits = 3) => Number.isFinite(n) ? n.toFixed(digits) : "—";
const count = (n) => Number(n).toLocaleString("en-ZA");
const utc = (value) => new Date(value).toISOString().slice(11, 19);
const colours = ["#4778aa", "#b68042", "#2d8068"];
let config, latest, history = [], plotMode = "history", windowSeconds = 300, archiveOffset = 0;
let lastEventAt = 0, connection, signalPoints = [], signalBounds, archiveBusy = false;
let weather, windHistory = [];
let control = {allowed: false}, loggingState = false, toggleBusy = false;
const selectedSessions = new Map();  // id -> capture count, kept across archive refreshes
let archiveItems = [];
const tokenKey = "oclab-control-token";
const ago = (s) => Number.isFinite(s) ? `${fmt(s, s < 10 ? 1 : 0)} s ago` : "no data";

async function fetchJSON(url, headers = {}) {
  const response = await fetch(url, {headers, signal: AbortSignal.timeout(15000)});
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function addHistory(items) {
  // Keyed by receive time: live captures shown while logging is off have no capture_id.
  const byTime = new Map(history.map((s) => [s.host_received_ns, s]));
  for (const item of items) byTime.set(item.host_received_ns, item);
  history = [...byTime.values()].sort((a, b) => a.host_received_ns - b.host_received_ns).slice(-7200);
}

function renderSnapshot(snapshot) {
  const status = snapshot.status;
  lastEventAt = Date.now();
  const healthy = status.acquiring, saving = healthy && status.logging;
  loggingState = status.logging;
  updateToggle();
  document.body.classList.toggle("stale", !healthy);
  $("status-dot").className = `dot ${saving ? "live" : healthy ? "idle" : "error"}`;
  $("status-dot").closest(".status-strip").classList.toggle("error", !healthy);
  $("status-dot").closest(".status-strip").classList.toggle("idle", healthy && !saving);
  $("connection-label").textContent = saving ? (status.mode === "demo" ? "Demo running" : "Link recording") : healthy ? "Live · not logging" : status.state === "starting" ? "Connecting" : "Recording paused";
  $("status-message").textContent = status.message;
  $("mode-banner").hidden = status.mode !== "demo";
  $("record-state").textContent = saving ? "Saving to disk" : healthy ? "Not saving (live view only)" : "Recording paused";
  if (snapshot.latest) {
    latest = snapshot.latest;
    addHistory(snapshot.captures || [latest.summary]);
    const s = latest.summary;
    for (const axis of ["x", "y", "sum"]) {
      $(`${axis}-value`).textContent = fmt(s[`${axis}_mean_v`]);
      $(`${axis}-std`).textContent = `σ ${fmt(s[`${axis}_std_v`])} V`;
    }
    $("wander-value").textContent = fmt(s.wander_rms, 4);
    $("position-unit").textContent = s.position_unit;
    $("position-calibration").textContent = s.position_unit === "mm" ? "Calibrated" : "Uncalibrated";
    $("valid-fraction").textContent = `${fmt(s.valid_fraction * 100, 1)}%`;
    $("scintillation").textContent = fmt(s.scintillation_index, 4);
    // Fraction of wall time inside bursts over the selected window; the gaps are USB transfer and re-arming.
    const recent = selectedHistory().filter((p) => Number.isFinite(p.since_previous_start_s));
    const span = recent.reduce((a, p) => a + p.since_previous_start_s, 0);
    $("coverage").textContent = span > 0 ? `${fmt(recent.reduce((a, p) => a + p.burst_duration_s, 0) / span * 100, 0)}%` : "—";
    // An unconnected spare channel should read 0 V; a few ADC codes away means a scope zero offset.
    const spareLimit = s.unused_channel && config ? 3 * config.scope.volts_per_div[s.unused_channel - 1] / 31 : Infinity;
    $("quality-note").textContent = Math.abs(s.unused_channel_mean_v) > spareLimit ? `CH${s.unused_channel} reads ${fmt(s.unused_channel_mean_v, 2)} V: scope zero offset? (if unconnected)`
      : s.saturated_fraction > 0 ? `${fmt(s.saturated_fraction * 100, 1)}% detector saturated · reduce light on the sensor` : s.clipped_fraction > 0 ? `${fmt(s.clipped_fraction * 100, 1)}% clipped · check ranges` : s.low_sum_fraction > 0 ? `${fmt(s.low_sum_fraction * 100, 1)}% below SUM threshold` : "No clipping or low-SUM flags";
    $("last-update").textContent = `${healthy ? "Updated" : "Last capture"} ${utc(s.host_received_utc)} UTC · ${fmt(status.last_capture_age_s, 0)} s ago`;
    $("centroid").textContent = `X ${fmt(s.position_x)} / Y ${fmt(s.position_y)}`;
    if (s.weather_status) {
      $("weather-capture").textContent = `Last capture: weather ${s.weather_status} · wind ${fmt(s.wind_mean_ms, 1)} ± ${fmt(s.wind_std_ms, 1)} m/s (n ${s.wind_n}) · TI ${fmt(s.wind_ti, 2)} · ${fmt(s.air_temp_c, 1)} °C`;
    }
  } else {
    $("last-update").textContent = "No capture yet";
  }
  if (snapshot.weather) renderWeather(snapshot.weather);
  drawCharts();
}

function renderWeather(w) {
  weather = w;
  const env = w.env || {}, wind = w.wind || {}, last = w.wind_last_1s;
  const live = w.enabled && !w.stale && w.state === "receiving";
  $("weather").classList.toggle("stale", !live);
  $("weather-tag").textContent = !w.enabled ? "DISABLED" : w.source === "demo" ? "SYNTHETIC · DEMO" : w.port ? `PICO W · ${w.port.split("/").pop()}` : "PICO W · NOT FOUND";
  $("weather-tag").classList.toggle("warn", w.enabled && !live);
  $("wx-wind").textContent = fmt(last ? last.mean_ms : wind.wind_ms, 1);
  $("wx-wind-note").textContent = wind.clip === 1 ? "ADC clipped · wind above range" : last ? `1 s mean · max ${fmt(last.max_ms, 1)} · ${last.n} samples` : "Latest sample";
  $("wx-air").textContent = fmt(env.baro_c, 1);
  // DHT11 humidity when it has one; otherwise BME280 (the DHT11 cannot read below about 20 %RH).
  const dhtRh = Number.isFinite(env.dht_rh), baroRh = Number.isFinite(env.baro_rh);
  $("wx-rh").textContent = fmt(dhtRh ? env.dht_rh : env.baro_rh, dhtRh ? 0 : 1);
  $("wx-rh-note").textContent = dhtRh ? `DHT11 · ${fmt(env.dht_c, 0)} °C · ±5 %RH` : baroRh ? `BME280 · ±3 %RH · DHT11 ${Number.isFinite(env.dht_c) ? "below range" : "no reading"}` : "DHT11 / BME280 · no reading";
  $("wx-pressure").textContent = fmt(env.baro_hpa, 1);
  $("wx-light").textContent = fmt(env.light_v, 2);
  $("wx-light-note").textContent = w.header?.light === "bpw34" ? `BPW34 · ${fmt(env.light_x, 1)} µA` : `LDR · ${Number.isFinite(env.light_x) ? fmt(env.light_x / 1000, 1) + " kΩ" : "—"}`;
  const skipped = w.counters?.malformed ? ` · ${count(w.counters.malformed)} malformed lines skipped` : "";
  $("weather-status").textContent = w.enabled ? `${w.message} · wind ${ago(w.wind_age_s)} · environment ${ago(w.env_age_s)}${skipped}` : w.message;
  if (last && (!windHistory.length || last.t_ns / 1e9 > windHistory[windHistory.length - 1].t + 0.5)) {
    windHistory.push({t: last.t_ns / 1e9, mean: last.mean_ms, max: last.max_ms});
    if (windHistory.length > 3600) windHistory = windHistory.slice(-3600);
  }
}

function offline() {
  document.body.classList.add("stale");
  $("status-dot").className = "dot error";
  $("status-dot").closest(".status-strip").classList.add("error");
  $("connection-label").textContent = "Connection lost";
  $("status-message").textContent = "Reconnecting to the service. Displayed values may be out of date.";
  $("record-state").textContent = "Recorder status unknown";
  $("weather").classList.add("stale");
  $("weather-status").textContent = "Weather status unknown while reconnecting to the service.";
}

function connect() {
  if (connection) connection.close();
  connection = new EventSource("/api/events");
  connection.addEventListener("snapshot", (event) => {
    try { renderSnapshot(JSON.parse(event.data)); } catch (error) { console.error(error); offline(); }
  });
  connection.onerror = offline;
}

function surface(id) {
  const canvas = $(id), rect = canvas.getBoundingClientRect(), scale = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * scale);
  canvas.height = Math.round(rect.height * scale);
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  ctx.font = "9px Arial";
  return {ctx, w: rect.width, h: rect.height};
}

function selectedHistory() {
  const end = history.length ? history[history.length - 1].host_received_ns / 1e9 : Date.now() / 1000;
  return history.filter((s) => s.source === config?.mode && s.session_id === latest?.summary.session_id && s.host_received_ns / 1e9 >= end - windowSeconds);
}

function drawSignal() {
  const {ctx, w, h} = surface("signal-chart");
  const margin = {l: 46, r: 17, t: 20, b: 31};
  const width = w - margin.l - margin.r, height = h - margin.t - margin.b;
  let points, xMin, xMax;
  if (plotMode === "waveform" && latest) {
    const p = latest.preview;
    points = p.time_s.map((t, i) => ({t: t * 1000, values: [p.x_v[i], p.y_v[i], p.sum_v[i]]}));
    xMin = 0; xMax = latest.summary.burst_duration_s * 1000;
  } else {
    points = selectedHistory().map((s) => ({t: s.host_received_ns / 1e9, values: [s.x_mean_v, s.y_mean_v, s.sum_mean_v], gap: s.since_previous_start_s, wind: s.wind_mean_ms}));
    xMax = points.length ? points[points.length - 1].t : Date.now() / 1000;
    xMin = xMax - windowSeconds;
  }
  signalPoints = points;
  signalBounds = {left: margin.l, width, min: xMin, max: xMax};
  $("signal-empty").hidden = points.length > 0;
  $("chart-count").textContent = plotMode === "waveform" ? `${points.length} preview points` : `${count(points.length)} captures`;
  let low = 0, high = 0;
  for (const point of points) for (const v of point.values) { low = Math.min(low, v); high = Math.max(high, v); }
  const padding = Math.max((high - low) * 0.13, 0.1);
  low -= padding; high += padding;
  const X = (t) => margin.l + (t - xMin) / (xMax - xMin) * width;
  const Y = (v) => margin.t + (high - v) / (high - low) * height;
  ctx.fillStyle = "#819097"; ctx.textAlign = "left"; ctx.fillText("V", 10, 12);
  for (let i = 0; i <= 4; i++) {
    const v = low + (high - low) * i / 4, y = Y(v);
    ctx.strokeStyle = "#e9eded"; ctx.beginPath(); ctx.moveTo(margin.l, y); ctx.lineTo(w - margin.r, y); ctx.stroke();
    ctx.fillStyle = "#8a969b"; ctx.textAlign = "right"; ctx.fillText(v.toFixed(2), margin.l - 9, y + 3);
  }
  for (let i = 0; i <= 4; i++) {
    const t = xMin + (xMax - xMin) * i / 4, x = X(t);
    ctx.strokeStyle = "#f0f2f1"; ctx.beginPath(); ctx.moveTo(x, margin.t); ctx.lineTo(x, h - margin.b); ctx.stroke();
    ctx.fillStyle = "#8a969b"; ctx.textAlign = i === 0 ? "left" : i === 4 ? "right" : "center";
    ctx.fillText(plotMode === "waveform" ? `${t.toFixed(1)} ms` : utc(t * 1000), x, h - 10);
  }
  for (let channel = 0; channel < 3; channel++) {
    ctx.strokeStyle = colours[channel]; ctx.lineWidth = 1.5; ctx.beginPath();
    points.forEach((point, i) => {
      const hasGap = plotMode === "history" && i > 0 && (point.t - points[i - 1].t > Math.max(3 * (config?.capture_interval_s || 0.5), 2));
      if (!i || hasGap) ctx.moveTo(X(point.t), Y(point.values[channel]));
      else ctx.lineTo(X(point.t), Y(point.values[channel]));
    });
    ctx.stroke();
    if (points.length) {
      const p = points[points.length - 1]; ctx.fillStyle = colours[channel]; ctx.beginPath(); ctx.arc(X(p.t), Y(p.values[channel]), 2.5, 0, Math.PI * 2); ctx.fill();
    }
  }
}

function drawWander() {
  const {ctx, w, h} = surface("wander-chart");
  const points = selectedHistory().filter((s) => Number.isFinite(s.position_x) && Number.isFinite(s.position_y)).slice(-1800);
  $("wander-empty").hidden = points.length > 0;
  const side = Math.min(w - 76, h - 53), left = (w - side) / 2 + 8, top = 18;
  let extent = latest?.summary.position_unit === "mm" ? 0.1 : 0.05;
  for (const p of points) extent = Math.max(extent, Math.abs(p.position_x) * 1.15, Math.abs(p.position_y) * 1.15);
  const X = (v) => left + (v / extent + 1) * side / 2;
  const Y = (v) => top + (1 - v / extent) * side / 2;
  for (let i = -2; i <= 2; i++) {
    const v = extent * i / 2;
    ctx.strokeStyle = i === 0 ? "#cfd8d7" : "#eaf0ee"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(X(v), top); ctx.lineTo(X(v), top + side); ctx.moveTo(left, Y(v)); ctx.lineTo(left + side, Y(v)); ctx.stroke();
    ctx.fillStyle = "#8a969b"; ctx.textAlign = "center"; ctx.fillText(v.toFixed(2), X(v), top + side + 15);
    ctx.textAlign = "right"; ctx.fillText(v.toFixed(2), left - 7, Y(v) + 3);
  }
  ctx.fillStyle = "#819097"; ctx.textAlign = "left"; ctx.fillText(`Y (${latest?.summary.position_unit || "ratio"})`, left, 10);
  ctx.textAlign = "center"; ctx.fillText(`X (${latest?.summary.position_unit || "ratio"})`, left + side / 2, top + side + 29);
  points.forEach((p, i) => {
    ctx.globalAlpha = 0.12 + (i / points.length) * 0.55;
    ctx.fillStyle = "#2d8068"; ctx.beginPath(); ctx.arc(X(p.position_x), Y(p.position_y), 2, 0, Math.PI * 2); ctx.fill();
  });
  ctx.globalAlpha = 1;
  if (points.length) {
    const p = points[points.length - 1]; ctx.strokeStyle = "#1e624f"; ctx.fillStyle = "#2d8068";
    ctx.beginPath(); ctx.arc(X(p.position_x), Y(p.position_y), 3.5, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(X(p.position_x), Y(p.position_y), 7, 0, Math.PI * 2); ctx.stroke();
  }
}

function drawWind() {
  const {ctx, w, h} = surface("wind-chart");
  const margin = {l: 46, r: 17, t: 20, b: 31};
  const width = w - margin.l - margin.r, height = h - margin.t - margin.b;
  const xMax = windHistory.length ? windHistory[windHistory.length - 1].t : Date.now() / 1000, xMin = xMax - windowSeconds;
  const points = windHistory.filter((p) => p.t >= xMin);
  $("wind-empty").hidden = points.length > 0;
  let high = 5;
  for (const p of points) high = Math.max(high, p.max * 1.15);
  const X = (t) => margin.l + (t - xMin) / (xMax - xMin) * width;
  const Y = (v) => margin.t + (high - v) / high * height;
  ctx.fillStyle = "#819097"; ctx.textAlign = "left"; ctx.fillText("m/s", 10, 12);
  for (let i = 0; i <= 4; i++) {
    const v = high * i / 4, y = Y(v);
    ctx.strokeStyle = "#e9eded"; ctx.beginPath(); ctx.moveTo(margin.l, y); ctx.lineTo(w - margin.r, y); ctx.stroke();
    ctx.fillStyle = "#8a969b"; ctx.textAlign = "right"; ctx.fillText(v.toFixed(1), margin.l - 9, y + 3);
  }
  for (let i = 0; i <= 4; i++) {
    const t = xMin + (xMax - xMin) * i / 4, x = X(t);
    ctx.strokeStyle = "#f0f2f1"; ctx.beginPath(); ctx.moveTo(x, margin.t); ctx.lineTo(x, h - margin.b); ctx.stroke();
    ctx.fillStyle = "#8a969b"; ctx.textAlign = i === 0 ? "left" : i === 4 ? "right" : "center"; ctx.fillText(utc(t * 1000), x, h - 10);
  }
  for (const [key, colour, lineWidth] of [["max", "#9fb3bf", 1], ["mean", "#3f6177", 1.5]]) {
    ctx.strokeStyle = colour; ctx.lineWidth = lineWidth; ctx.beginPath();
    points.forEach((p, i) => {
      // Missing seconds (disconnects) are gaps, never interpolated.
      if (!i || p.t - points[i - 1].t > 3) ctx.moveTo(X(p.t), Y(p[key])); else ctx.lineTo(X(p.t), Y(p[key]));
    });
    ctx.stroke();
  }
  if (points.length) {
    const p = points[points.length - 1]; ctx.fillStyle = "#3f6177"; ctx.beginPath(); ctx.arc(X(p.t), Y(p.mean), 2.5, 0, Math.PI * 2); ctx.fill();
  }
}

function drawCharts() { drawSignal(); drawWander(); drawWind(); }

function storedToken() { try { return sessionStorage.getItem(tokenKey) || ""; } catch (error) { return ""; } }
function saveToken(token) { try { if (token) sessionStorage.setItem(tokenKey, token); else sessionStorage.removeItem(tokenKey); } catch (error) { /* private mode */ } }

async function loadControl() {
  try {
    control = await fetchJSON("/api/control", storedToken() ? {"X-Control-Token": storedToken()} : {});
    if (!control.allowed && storedToken()) saveToken("");
  } catch (error) { control = {allowed: false, reason: "Logging control unavailable"}; }
  updateToggle();
  // Deleting uses the same permission as the logging switch; public visitors never see it.
  for (const el of document.querySelectorAll(".select-col")) el.hidden = !control.allowed;
  updateDeleteButton();
}

function controlHeaders(json = false) {
  const headers = {"X-OCLab-Control": "1"};
  if (json) headers["Content-Type"] = "application/json";
  if (storedToken()) headers["X-Control-Token"] = storedToken();
  return headers;
}

function updateDeleteButton() {
  const button = $("delete-sessions"), n = selectedSessions.size;
  button.hidden = !control.allowed;
  button.disabled = n === 0;
  button.textContent = n ? `Delete selected (${n})` : "Delete selected";
  const boxes = archiveItems.filter((item) => item.status !== "recording");
  $("select-all-sessions").checked = boxes.length > 0 && boxes.every((item) => selectedSessions.has(item.id));
}

async function deleteSelected() {
  const ids = [...selectedSessions.keys()];
  const captures = [...selectedSessions.values()].reduce((a, b) => a + b, 0);
  if (!ids.length || !window.confirm(`Permanently delete ${ids.length} session${ids.length > 1 ? "s" : ""} (${count(captures)} captures)?\n\nThis cannot be undone. Weather records are kept.`)) return;
  $("delete-sessions").disabled = true;
  const failed = [];
  for (const id of ids) {
    try {
      const response = await fetch(`/api/sessions/${encodeURIComponent(id)}`, {method: "DELETE", headers: controlHeaders(), signal: AbortSignal.timeout(60000)});
      if (response.ok || response.status === 404) selectedSessions.delete(id);
      else failed.push(`${id}: ${(await response.json()).detail || response.status}`);
    } catch (error) { failed.push(`${id}: ${error.message}`); }
  }
  if (failed.length) window.alert(`Not deleted:\n${failed.join("\n")}`);
  archiveOffset = 0;
  await loadArchive();
  loadExploreSessions();
}

function updateToggle() {
  const button = $("logging-toggle");
  button.setAttribute("aria-checked", String(Boolean(loggingState)));
  $("logging-label").textContent = loggingState ? "Logging on" : "Logging off";
  button.disabled = toggleBusy || !(control.allowed || control.token_available);
  button.title = control.allowed ? (loggingState ? "Stop saving data (closes the current session)" : "Start saving data to disk")
    : control.token_available ? "Enter the control token to switch logging" : (control.reason || "");
}

async function toggleLogging() {
  if (!control.allowed) {
    const token = window.prompt("Control token for this station:");
    if (!token) return;
    saveToken(token);
    await loadControl();
    if (!control.allowed) { window.alert(control.reason || "Token not accepted"); return; }
  }
  const enable = !loggingState;
  if (!enable && !window.confirm("Stop saving data? The current session will be closed; the live view continues.")) return;
  toggleBusy = true; updateToggle();
  try {
    const headers = controlHeaders(true);
    const response = await fetch("/api/logging", {method: "POST", headers, body: JSON.stringify({enabled: enable}), signal: AbortSignal.timeout(15000)});
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `request failed (${response.status})`);
    loggingState = body.logging;
    setTimeout(loadArchive, 2000);
  } catch (error) {
    $("status-message").textContent = `Could not switch logging: ${error.message}`;
  } finally { toggleBusy = false; updateToggle(); }
}

function cell(row, content, className) {
  const td = document.createElement("td");
  if (className) td.className = className;
  if (content instanceof Node) td.append(content); else td.textContent = content;
  row.append(td); return td;
}

async function loadArchive() {
  if (archiveBusy) return;
  archiveBusy = true;
  $("refresh-archive").disabled = true;
  try {
    const data = await fetchJSON(`/api/sessions?limit=8&offset=${archiveOffset}`);
    archiveItems = data.items;
    const body = $("sessions-body"); body.replaceChildren();
    if (!data.items.length) {
      const row = document.createElement("tr");
      cell(row, "No recordings yet. Sessions appear when the first waveform is saved.", "empty-table").colSpan = 7; body.append(row);
    }
    for (const item of data.items) {
      const row = document.createElement("tr");
      const box = document.createElement("input");
      box.type = "checkbox"; box.checked = selectedSessions.has(item.id);
      box.disabled = item.status === "recording";
      box.title = box.disabled ? "Still recording: stop logging before deleting" : "Select to delete";
      box.setAttribute("aria-label", `Select session ${item.id}`);
      box.onchange = () => { if (box.checked) selectedSessions.set(item.id, item.capture_count); else selectedSessions.delete(item.id); updateDeleteButton(); };
      cell(row, box, "select-col").hidden = !control.allowed;
      const started = cell(row, new Date(item.started_utc).toISOString().slice(0, 19).replace("T", " "));
      const id = document.createElement("small"); id.textContent = item.id; started.append(id);
      cell(row, item.mode === "demo" ? "Demo" : "Hardware", `table-source ${item.mode}`);
      cell(row, item.status[0].toUpperCase() + item.status.slice(1), `session-state ${item.status}`);
      cell(row, count(item.capture_count)); cell(row, count(item.sample_count));
      const links = document.createElement("div"); links.className = "download-links";
      for (const [file, label] of [["summary.csv", "CSV ↓"], ["raw.csv.gz", "Raw ↓"], ["minutes.csv", "Minutes ↓"], ["session.npz", "NPZ ↓"], ["weather.csv", "Weather ↓"], ["metadata.json", "Metadata ↓"]]) {
        const a = document.createElement("a"); a.href = `/api/sessions/${encodeURIComponent(item.id)}/${file}`; a.textContent = label;
        a.setAttribute("aria-label", `Download ${file} for session ${item.id}`); a.download = `${item.id}-${file}`; links.append(a);
      }
      const plot = document.createElement("button");
      plot.className = "plot-link"; plot.textContent = "Plot ↘"; plot.setAttribute("aria-label", `Plot session ${item.id}`);
      plot.onclick = () => openInExplorer(item.id);
      links.append(plot);
      cell(row, links); body.append(row);
    }
    updateDeleteButton();
    $("archive-count").textContent = data.total ? `${archiveOffset + 1}–${archiveOffset + data.items.length} of ${count(data.total)} sessions · times in UTC` : "0 sessions";
    $("previous-page").disabled = archiveOffset === 0;
    $("next-page").disabled = archiveOffset + data.items.length >= data.total;
  } catch (error) {
    $("archive-count").textContent = "Could not load the archive. Use Refresh archive to retry.";
  } finally { archiveBusy = false; $("refresh-archive").disabled = false; }
}

function setPlot(mode) {
  plotMode = mode;
  for (const [id, selected] of [["trend-tab", mode === "history"], ["waveform-tab", mode === "waveform"]]) {
    $(id).classList.toggle("selected", selected); $(id).setAttribute("aria-pressed", String(selected));
  }
  $("signal-caption").textContent = mode === "history" ? "Capture means · time in UTC · gaps are not interpolated" : "Reduced preview · full waveform retained in archive";
  $("chart-tooltip").hidden = true; drawCharts();
}

$("trend-tab").onclick = () => setPlot("history");
$("waveform-tab").onclick = () => setPlot("waveform");
$("time-window").onchange = (event) => { windowSeconds = Number(event.target.value); drawCharts(); };
$("refresh-archive").onclick = loadArchive;
$("logging-toggle").onclick = toggleLogging;
$("delete-sessions").onclick = deleteSelected;
$("select-all-sessions").onchange = (event) => {
  for (const item of archiveItems) {
    if (item.status === "recording") continue;
    if (event.target.checked) selectedSessions.set(item.id, item.capture_count); else selectedSessions.delete(item.id);
  }
  for (const box of document.querySelectorAll("#sessions-body input[type=checkbox]:not(:disabled)")) box.checked = event.target.checked;
  updateDeleteButton();
};
$("previous-page").onclick = () => { if (!archiveBusy) { archiveOffset = Math.max(0, archiveOffset - 8); loadArchive(); } };
$("next-page").onclick = () => { if (!archiveBusy) { archiveOffset += 8; loadArchive(); } };
$("signal-chart").addEventListener("pointermove", (event) => {
  if (!signalPoints.length || !signalBounds) return;
  const x = event.clientX - event.target.getBoundingClientRect().left;
  const t = signalBounds.min + (x - signalBounds.left) / signalBounds.width * (signalBounds.max - signalBounds.min);
  const p = signalPoints.reduce((a, b) => Math.abs(a.t - t) < Math.abs(b.t - t) ? a : b);
  $("chart-tooltip").textContent = `${plotMode === "waveform" ? p.t.toFixed(2) + " ms" : utc(p.t * 1000) + " UTC"} | X ${fmt(p.values[0])} · Y ${fmt(p.values[1])} · SUM ${fmt(p.values[2])} V${p.wind !== undefined ? ` · wind ${fmt(p.wind, 1)} m/s` : ""}`;
  $("chart-tooltip").hidden = false;
});
$("signal-chart").addEventListener("pointerleave", () => { $("chart-tooltip").hidden = true; });
new ResizeObserver(drawCharts).observe($("signal-chart").parentElement);
new ResizeObserver(drawWander).observe($("wander-chart").parentElement);
new ResizeObserver(drawWind).observe($("wind-chart").parentElement);
setInterval(() => {
  $("clock").textContent = `${new Date().toISOString().slice(0, 10)} / ${utc(Date.now())} UTC`;
  if (lastEventAt && Date.now() - lastEventAt > 6000) offline();
}, 1000);
setInterval(loadArchive, 15000);
window.addEventListener("pagehide", () => connection?.close());
window.addEventListener("pageshow", (event) => { if (event.persisted) connect(); });

// ---- Explore recorded data --------------------------------------------------
// One panel per variable on a shared time axis (never two y-scales on one axis),
// or a scatter of one variable against another.
const EXPLORE_COLOUR = "#4778aa", EXPLORE_BAND = "rgba(71, 120, 170, 0.16)";
let plotFields = {}, exploreLayout = null, exploreWidth = 0;
const explore = {mode: "time", vars: ["x_mean_v", "y_mean_v", "sum_mean_v"], x: "position_x", y: "position_y", data: null, request: 0};

function fieldLabel(name, units) {
  const unit = units?.[name] ?? plotFields[name]?.unit;
  return `${plotFields[name]?.label || name}${unit ? ` (${unit})` : ""}`;
}

function tick(value, span) {
  const digits = Math.max(0, Math.min(5, Math.ceil(-Math.log10(span || 1)) + 2));
  return value.toFixed(digits);
}

function saveFile(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

async function loadExploreSessions() {
  try {
    const data = await fetchJSON("/api/sessions?limit=100");
    const select = $("explore-session"), current = select.value;
    select.replaceChildren();
    if (!data.items.length) select.append(new Option("No recorded sessions yet", ""));
    for (const item of data.items) {
      const start = new Date(item.started_utc).toISOString().slice(0, 16).replace("T", " ");
      select.append(new Option(`${start} UTC · ${item.mode === "demo" ? "Demo" : "Hardware"} · ${count(item.capture_count)} captures · ${item.status}`, item.id));
    }
    select.value = [...select.options].some((o) => o.value === current) ? current : select.options[0].value;
    if (select.value !== current || !explore.data) loadExplore();
  } catch (error) { $("explore-empty").textContent = "Could not load the session list"; }
}

function openInExplorer(id) {
  const select = $("explore-session");
  if (![...select.options].some((o) => o.value === id)) select.append(new Option(id, id));
  select.value = id;
  loadExplore();
  $("explore").scrollIntoView({block: "start"});
}

function buildVarChips() {
  const box = $("explore-vars");
  box.querySelectorAll("label").forEach((label) => label.remove());
  for (const [name, field] of Object.entries(plotFields)) {
    const label = document.createElement("label"), input = document.createElement("input");
    label.className = "var-chip"; input.type = "checkbox"; input.value = name; input.checked = explore.vars.includes(name);
    input.onchange = () => {
      if (input.checked && explore.vars.length >= 6) { input.checked = false; return; }
      explore.vars = input.checked ? [...explore.vars, name] : explore.vars.filter((v) => v !== name);
      loadExplore();
    };
    label.append(input, field.label); box.append(label);
  }
  for (const id of ["explore-x", "explore-y"]) {
    const select = $(id); select.replaceChildren();
    for (const [name, field] of Object.entries(plotFields)) select.append(new Option(field.label, name));
  }
  $("explore-x").value = explore.x; $("explore-y").value = explore.y;
}

function setExploreMode(mode) {
  explore.mode = mode;
  for (const [id, on] of [["explore-mode-time", mode === "time"], ["explore-mode-xy", mode === "xy"]]) {
    $(id).classList.toggle("selected", on); $(id).setAttribute("aria-pressed", String(on));
  }
  $("explore-vars").hidden = mode !== "time"; $("explore-xy").hidden = mode !== "xy";
  loadExplore();
}

async function loadExplore() {
  const session = $("explore-session").value;
  const fields = explore.mode === "time" ? explore.vars : [...new Set([explore.x, explore.y])];
  const ticket = ++explore.request;
  explore.data = null;
  if (!session || !fields.length) { drawExplore(); return; }
  $("explore-empty").textContent = "Loading…"; $("explore-empty").hidden = false;
  try {
    const data = await fetchJSON(`/api/sessions/${encodeURIComponent(session)}/series?fields=${fields.join(",")}&points=2000`);
    if (ticket !== explore.request) return;  // Superseded by a newer choice
    explore.data = data;
  } catch (error) {
    if (ticket !== explore.request) return;
    $("explore-empty").textContent = `Could not load the session (${error.message})`;
    return;
  }
  drawExplore();
}

function exploreSurface(height) {
  $("explore-chart").style.height = `${height}px`;
  const s = surface("explore-chart");
  s.ctx.fillStyle = "#ffffff"; s.ctx.fillRect(0, 0, s.w, s.h);  // Opaque for PNG export
  return s;
}

function drawExplore(hover = null) {
  const d = explore.data;
  const fields = !d ? [] : explore.mode === "time" ? explore.vars.filter((f) => d.fields[f]) : [explore.x, explore.y];
  const ready = Boolean(d && d.t_ns.length && fields.length && fields.every((f) => d.fields[f]));
  $("explore-png").disabled = $("explore-csv").disabled = !ready;
  $("explore-count").textContent = d ? `${count(d.captures)} captures${d.binned ? ` · averaged into ${count(d.t_ns.length)} points (band = min to max)` : ""}` : "";
  if (!ready) {
    exploreSurface(260); exploreLayout = null;
    if (d) $("explore-empty").textContent = !d.t_ns.length ? "This session has no captures" : "Choose at least one variable";
    else if (!$("explore-session").value) $("explore-empty").textContent = "Choose a session to plot";
    $("explore-empty").hidden = false;
    return;
  }
  $("explore-empty").hidden = true;
  if (explore.mode === "time") drawExploreTime(d, fields, hover); else drawExploreXY(d, hover);
}

function drawExploreTime(d, fields, hover) {
  const rowH = 128, gap = 22, margin = {l: 62, r: 18, t: 6, b: 30};
  const {ctx, w, h} = exploreSurface(margin.t + fields.length * (rowH + gap) - gap + margin.b);
  const t = d.t_ns.map((x) => x / 1e9);
  const steps = t.slice(1).map((x, i) => x - t[i]).sort((a, b) => a - b);
  const typical = steps[Math.floor(steps.length / 2)] || 1;
  const gapLimit = 5 * typical;
  const broken = (i) => i > 0 && t[i] - t[i - 1] > gapLimit;
  // Long breaks (PC asleep, app stopped) collapse to a narrow strip so each recording stretch stays readable.
  const GAP_PX = 34, segments = [];
  let first = 0;
  for (let i = 1; i <= t.length; i++) {
    if (i === t.length || t[i] - t[i - 1] > Math.max(300, 10 * typical)) { segments.push([first, i - 1]); first = i; }
  }
  const width = w - margin.l - margin.r - GAP_PX * (segments.length - 1);
  const total = segments.reduce((a, [a0, a1]) => a + Math.max(t[a1] - t[a0], typical), 0);
  let cursor = margin.l;
  const spans = segments.map(([a0, a1]) => {
    const px = width * Math.max(t[a1] - t[a0], typical) / total, span = {t0: t[a0], t1: t[a1], x0: cursor, x1: cursor + px};
    cursor += px + GAP_PX;
    return span;
  });
  const X = (x) => {
    const span = spans.find((sp) => x <= sp.t1) || spans[spans.length - 1];
    return span.t1 === span.t0 ? (span.x0 + span.x1) / 2 : span.x0 + (x - span.t0) / (span.t1 - span.t0) * (span.x1 - span.x0);
  };
  exploreLayout = {mode: "time", t, X, fields};
  spans.slice(1).forEach((span, k) => {  // Shade and label each collapsed gap
    const x0 = spans[k].x1 + 4, x1 = span.x0 - 4, hours = (span.t0 - spans[k].t1) / 3600;
    ctx.fillStyle = "#f1f3f2"; ctx.fillRect(x0, margin.t, x1 - x0, h - margin.t - margin.b);
    ctx.save(); ctx.translate((x0 + x1) / 2 + 3, margin.t + 40); ctx.rotate(-Math.PI / 2);
    ctx.fillStyle = "#8a969b"; ctx.textAlign = "right"; ctx.font = "9px Arial";
    ctx.fillText(hours >= 1 ? `${hours.toFixed(1)} h gap` : `${Math.round(hours * 60)} min gap`, 0, 0); ctx.restore();
  });
  ctx.font = "9px Arial";
  fields.forEach((f, row) => {
    const top = margin.t + row * (rowH + gap), s = d.fields[f];
    ctx.fillStyle = "#1a3142"; ctx.textAlign = "left"; ctx.font = "10px Arial";
    ctx.fillText(fieldLabel(f, d.units), margin.l, top + 9); ctx.font = "9px Arial";
    const lows = s.min.filter(Number.isFinite), highs = s.max.filter(Number.isFinite);
    if (!lows.length) { ctx.fillStyle = "#8a969b"; ctx.fillText("No values recorded", margin.l, top + 40); return; }
    let lo = Math.min(...lows), hi = Math.max(...highs);
    if (hi - lo < 1e-12) { const pad = Math.abs(hi) * 0.05 || 0.5; lo -= pad; hi += pad; }
    const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    const plotTop = top + 16, plotH = rowH - 16;
    const Y = (v) => plotTop + (hi - v) / (hi - lo) * plotH;
    for (let i = 0; i <= 2; i++) {
      const v = lo + (hi - lo) * i / 2, y = Y(v);
      ctx.strokeStyle = "#e9eded"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(margin.l, y); ctx.lineTo(w - margin.r, y); ctx.stroke();
      ctx.fillStyle = "#8a969b"; ctx.textAlign = "right"; ctx.fillText(tick(v, hi - lo), margin.l - 8, y + 3);
    }
    if (d.binned) {  // min-max band per run of points without gaps
      ctx.fillStyle = EXPLORE_BAND;
      let run = [];
      const flush = () => {
        if (run.length > 1) {
          ctx.beginPath();
          run.forEach((i, k) => (k ? ctx.lineTo : ctx.moveTo).call(ctx, X(t[i]), Y(s.max[i])));
          [...run].reverse().forEach((i) => ctx.lineTo(X(t[i]), Y(s.min[i])));
          ctx.closePath(); ctx.fill();
        }
        run = [];
      };
      t.forEach((_, i) => { if (broken(i) || !Number.isFinite(s.min[i])) flush(); if (Number.isFinite(s.min[i])) run.push(i); });
      flush();
    }
    ctx.strokeStyle = EXPLORE_COLOUR; ctx.lineWidth = 1.5; ctx.beginPath();
    let pen = false;
    s.mean.forEach((v, i) => {
      if (!Number.isFinite(v)) { pen = false; return; }
      if (!pen || broken(i)) ctx.moveTo(X(t[i]), Y(v)); else ctx.lineTo(X(t[i]), Y(v));
      pen = true;
    });
    ctx.stroke();
    // A value with no drawable neighbour would be invisible as a line: show it as a dot.
    ctx.fillStyle = EXPLORE_COLOUR;
    s.mean.forEach((v, i) => {
      const joined = (j) => j >= 0 && j < t.length && Number.isFinite(s.mean[j]) && !broken(Math.max(i, j));
      if (Number.isFinite(v) && !joined(i - 1) && !joined(i + 1)) { ctx.beginPath(); ctx.arc(X(t[i]), Y(v), 2.5, 0, Math.PI * 2); ctx.fill(); }
    });
    if (hover !== null && Number.isFinite(s.mean[hover])) {
      ctx.fillStyle = EXPLORE_COLOUR; ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(X(t[hover]), Y(s.mean[hover]), 4, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    }
  });
  const axisY = h - margin.b + 18, multiDay = t[t.length - 1] - t[0] > 86400;
  const stamp = (x) => new Date(x * 1000).toISOString().slice(multiDay ? 5 : 11, 19).replace("T", " ");
  ctx.fillStyle = "#8a969b";
  spans.forEach((span, k) => {  // Start and end of each stretch, plus a middle tick when there is room
    const room = span.x1 - span.x0;
    if (k === 0 || room > 60) { ctx.textAlign = "left"; ctx.fillText(stamp(span.t0), span.x0, axisY); }
    if (room > 150) { ctx.textAlign = "right"; ctx.fillText(stamp(span.t1), span.x1, axisY); }
    if (room > 420) { ctx.textAlign = "center"; ctx.fillText(stamp((span.t0 + span.t1) / 2), (span.x0 + span.x1) / 2, axisY); }
  });
  if (hover !== null) {
    ctx.strokeStyle = "#9aa6ab"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(X(t[hover]), margin.t); ctx.lineTo(X(t[hover]), h - margin.b + 4); ctx.stroke();
  }
}

function drawExploreXY(d, hover) {
  const margin = {l: 64, r: 22, t: 14, b: 44};
  const box = $("explore-wrap").getBoundingClientRect();
  const {ctx, w, h} = exploreSurface(Math.max(300, Math.min(480, box.width * 0.55)));
  const xs = d.fields[explore.x].mean, ys = d.fields[explore.y].mean;
  const idx = xs.map((_, i) => i).filter((i) => Number.isFinite(xs[i]) && Number.isFinite(ys[i]));
  ctx.font = "9px Arial";
  if (!idx.length) { exploreLayout = null; ctx.fillStyle = "#8a969b"; ctx.textAlign = "center"; ctx.fillText("No captures have both values", w / 2, h / 2); return; }
  const range = (a) => { let lo = Math.min(...a), hi = Math.max(...a); if (hi - lo < 1e-12) { const p = Math.abs(hi) * 0.05 || 0.5; lo -= p; hi += p; } const p = (hi - lo) * 0.06; return [lo - p, hi + p]; };
  const [xlo, xhi] = range(idx.map((i) => xs[i])), [ylo, yhi] = range(idx.map((i) => ys[i]));
  const X = (v) => margin.l + (v - xlo) / (xhi - xlo) * (w - margin.l - margin.r);
  const Y = (v) => margin.t + (yhi - v) / (yhi - ylo) * (h - margin.t - margin.b);
  for (let i = 0; i <= 4; i++) {
    const vx = xlo + (xhi - xlo) * i / 4, vy = ylo + (yhi - ylo) * i / 4;
    ctx.strokeStyle = "#e9eded"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(X(vx), margin.t); ctx.lineTo(X(vx), h - margin.b); ctx.moveTo(margin.l, Y(vy)); ctx.lineTo(w - margin.r, Y(vy)); ctx.stroke();
    ctx.fillStyle = "#8a969b"; ctx.textAlign = "center"; ctx.fillText(tick(vx, xhi - xlo), X(vx), h - margin.b + 14);
    ctx.textAlign = "right"; ctx.fillText(tick(vy, yhi - ylo), margin.l - 8, Y(vy) + 3);
  }
  ctx.fillStyle = "#52616c"; ctx.font = "10px Arial"; ctx.textAlign = "center";
  ctx.fillText(fieldLabel(explore.x, d.units), margin.l + (w - margin.l - margin.r) / 2, h - 8);
  ctx.save(); ctx.translate(14, margin.t + (h - margin.t - margin.b) / 2); ctx.rotate(-Math.PI / 2); ctx.fillText(fieldLabel(explore.y, d.units), 0, 0); ctx.restore();
  ctx.fillStyle = EXPLORE_COLOUR; ctx.globalAlpha = 0.45;
  for (const i of idx) { ctx.beginPath(); ctx.arc(X(xs[i]), Y(ys[i]), 2.5, 0, Math.PI * 2); ctx.fill(); }
  ctx.globalAlpha = 1;
  if (hover !== null) {
    ctx.fillStyle = EXPLORE_COLOUR; ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(X(xs[hover]), Y(ys[hover]), 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  }
  exploreLayout = {mode: "xy", points: idx.map((i) => [X(xs[i]), Y(ys[i]), i])};
}

function exploreHover(event) {
  const d = explore.data;
  if (!exploreLayout || !d) return;
  const rect = event.target.getBoundingClientRect(), mx = event.clientX - rect.left, my = event.clientY - rect.top;
  let index = null;
  if (exploreLayout.mode === "time") {
    let best = Infinity;
    exploreLayout.t.forEach((x, i) => { const dist = Math.abs(exploreLayout.X(x) - mx); if (dist < best) { best = dist; index = i; } });
  } else {
    let best = 14 ** 2;  // Hit radius larger than the mark
    for (const [px, py, i] of exploreLayout.points) { const dist = (px - mx) ** 2 + (py - my) ** 2; if (dist < best) { best = dist; index = i; } }
  }
  drawExplore(index);
  if (index === null) { $("explore-tooltip").hidden = true; return; }
  const fields = exploreLayout.mode === "time" ? exploreLayout.fields : [explore.x, explore.y];
  const lines = [`${new Date(d.t_ns[index] / 1e6).toISOString().slice(0, 19).replace("T", " ")} UTC`];
  for (const f of [...new Set(fields)]) {
    const s = d.fields[f], v = s.mean[index];
    lines.push(`${fieldLabel(f, d.units)}: ${Number.isFinite(v) ? v.toPrecision(5) : "—"}${d.binned && Number.isFinite(v) ? `  [${s.min[index].toPrecision(4)} to ${s.max[index].toPrecision(4)}]` : ""}`);
  }
  $("explore-tooltip").textContent = lines.join("\n"); $("explore-tooltip").hidden = false;
}

function exploreCsv() {
  const d = explore.data;
  const fields = [...new Set(explore.mode === "time" ? explore.vars.filter((f) => d.fields[f]) : [explore.x, explore.y])];
  const header = ["utc", "t_ns"];
  for (const f of fields) header.push(f, ...(d.binned ? [`${f}_min`, `${f}_max`] : []));
  const rows = d.t_ns.map((ns, i) => {
    const row = [new Date(ns / 1e6).toISOString(), ns];
    for (const f of fields) row.push(d.fields[f].mean[i] ?? "", ...(d.binned ? [d.fields[f].min[i] ?? "", d.fields[f].max[i] ?? ""] : []));
    return row.join(",");
  });
  saveFile(new Blob([[header.join(","), ...rows].join("\r\n") + "\r\n"], {type: "text/csv"}), `${d.session_id}-${explore.mode === "time" ? "time" : "xy"}.csv`);
}

$("explore-session").onchange = loadExplore;
$("explore-mode-time").onclick = () => setExploreMode("time");
$("explore-mode-xy").onclick = () => setExploreMode("xy");
$("explore-x").onchange = (event) => { explore.x = event.target.value; loadExplore(); };
$("explore-y").onchange = (event) => { explore.y = event.target.value; loadExplore(); };
$("explore-chart").addEventListener("pointermove", exploreHover);
$("explore-chart").addEventListener("pointerleave", () => { $("explore-tooltip").hidden = true; drawExplore(); });
$("explore-png").onclick = () => $("explore-chart").toBlob((blob) => saveFile(blob, `${explore.data.session_id}-${explore.mode === "time" ? "time" : "xy"}.png`));
$("explore-csv").onclick = exploreCsv;
new ResizeObserver(() => {
  const width = $("explore-wrap").getBoundingClientRect().width;
  if (Math.abs(width - exploreWidth) > 1) { exploreWidth = width; drawExplore(); }  // Width only: our height changes must not loop
}).observe($("explore-wrap"));

async function init() {
  drawCharts();
  try {
    config = await fetchJSON("/api/config");
    $("mode-banner").hidden = config.mode !== "demo";
    const s = config.scope, c = config.calibration, e = config.experiment;
    $("sample-rate").textContent = `${count(s.sample_rate)} Sa/s / channel`;
    $("burst-depth").textContent = `${count(s.depth)} samples · ${fmt(s.depth / s.sample_rate * 1000, 0)} ms`;
    const bandwidth = s.store_rate_hz === s.sample_rate ? s.store_rate_hz / 2 : s.store_rate_hz * 0.4;
    $("store-rate").textContent = `${count(s.store_rate_hz)} Sa/s · ${count(bandwidth)} Hz band`;
    [s.x_channel, s.y_channel, s.sum_channel].forEach((channel, i) => { document.querySelectorAll(".channel")[i].textContent = `CH${channel}`; });
    $("scatter-tag").textContent = c.x_scale_mm !== null ? "CALIBRATED · mm" : c.position_model === "voltage" ? "X · Y (V)" : "X/SUM · Y/SUM";
    $("laser-name").textContent = `${e.laser} · ${e.wavelength_nm} nm`;
    $("path-length").textContent = `${e.one_way_distance_m * 2} m round trip`;
    $("detector-name").textContent = e.detector;
    $("license-note").textContent = `Reuse terms: ${e.data_license}${e.contact ? " · Contact: " + e.contact : ""}`;
    document.querySelector(".intro").textContent = `A view into light travelling through the atmosphere. Live measurements from our ${e.one_way_distance_m * 2} m folded laser link.`;
    const diagramTexts = document.querySelectorAll(".link-diagram text");
    diagramTexts[1].textContent = `${e.one_way_distance_m} m · outbound`;
    diagramTexts[2].textContent = `${e.one_way_distance_m} m · return`;
    document.querySelector(".link-diagram").setAttribute("aria-label", `Laser to mirror: ${e.one_way_distance_m} metres. Mirror to quadrant photodiode: ${e.one_way_distance_m} metres.`);
    document.querySelector(".experiment-copy p:not(.eyebrow)").textContent = `A continuous-wave laser travels ${e.one_way_distance_m} m to a mirror and returns to a quadrant photodiode. The breakout box provides X, Y and SUM signals to the USB oscilloscope.`;
    const data = await fetchJSON("/api/history?limit=7200");
    // Only the current source and latest session are comparable under current calibration.
    const newestSession = data.items.at(-1)?.session_id;
    addHistory(data.items.filter((item) => item.session_id === newestSession && item.source === config.mode));
    drawCharts();
  } catch (error) { offline(); }
  try {
    const recent = await fetchJSON("/api/weather/history?seconds=900");
    windHistory = recent.wind.map((p) => ({t: p.t_ns / 1e9, mean: p.mean_ms, max: p.max_ms}));
    drawWind();
  } catch (error) { console.error(error); }
  connect(); loadArchive(); loadControl();
  try {
    plotFields = await fetchJSON("/api/plot-fields");
    buildVarChips();
    setExploreMode("time");
  } catch (error) { $("explore-empty").textContent = "Could not load plot options"; }
  loadExploreSessions();
}
init();
